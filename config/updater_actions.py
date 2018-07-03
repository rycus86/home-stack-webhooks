from __future__ import print_function

import os
import re
import yaml
import docker
import hashlib
import requests

from io import BytesIO

from docker_helper import read_configuration

from actions import action, Action


@action('git-update')
class GitUpdateAction(Action):
    """
    Update the contents of a Git repo in a directory, and optionally unlock encrypted files.

    To do this, first a `debian` based image is built with the necessary tools installed.
    This then clones the project if it doesn't exist locally yet.
    The changes are pulled from Git.
    The encrypted files are decrypted using git-crypt optionally.

    This step always uses the local Docker endpoint.
    """

    def __init__(self, volumes, clone_url=None, check_dir=None, crypt_key=None, user=None):
        self.volumes = volumes
        self.clone_url = clone_url
        self.check_dir = check_dir
        self.crypt_key = crypt_key
        self.user = user

    def _run(self):
        dockerfile = """
        FROM debian:stable-slim
        RUN apt update && apt install -y git-core git-crypt openssl
        """

        client = docker.from_env()
        client.images.build(fileobj=BytesIO(dockerfile), rm=True, forcerm=True, tag='git-updater')

        print('Git updater image ready')

        volumes = map(self._render_with_template, self.volumes)
        check_dir = self._render_with_template(self.check_dir) if self.check_dir else None
        crypt_key = self._render_with_template(self.crypt_key) if self.crypt_key else None
        user = self._render_with_template(self.user) if self.user else None

        if check_dir and not os.path.exists('%s/.git' % check_dir):
            if not self.clone_url:
                self.error('Working directory does not contain a Git repository')

            clone_url = self._render_with_template(self.clone_url)

            print(client.containers.run(image='git-updater', command='git clone %s .' % clone_url,
                                        user=user, working_dir='/workdir',
                                        volumes=volumes, remove=True))

        print(client.containers.run(image='git-updater', command='git pull',
                                    user=user, working_dir='/workdir',
                                    volumes=volumes, remove=True))

        if crypt_key:
            print(client.containers.run(image='git-updater', command='git-crypt unlock %s' % crypt_key,
                                        user=user, working_dir='/workdir',
                                        volumes=volumes, remove=True))


@action('stack-prepare-networks')
class StackPrepareNetworks(Action):
    """
    Make sure all external networks defined in a stack YAML file exist.

    First, the stack YAML is parsed.
    For all the `external` networks (in the top-level `networks` mapping),
      the `overlay` network is created if it does not exist yet.
    """

    client = docker.from_env()

    def __init__(self, config_dir, stack_file='stack.yml'):
        self.config_dir = config_dir
        self.stack_file = stack_file

    def _run(self):
        config_dir = self._render_with_template(self.config_dir)
        stack_file = self._render_with_template(self.stack_file)

        with open('%s/%s' % (config_dir, stack_file)) as stack_yml:
            parsed = yaml.load(stack_yml.read())

        for network, config in parsed.get('networks', {}).items():
            if config.get('external', False):
                name = config.get('name', network)

                if not self.client.networks.list(names=[name]):
                    print('Creating new external overlay network: %s' % name)

                    created = self.client.networks.create(name, driver='overlay', attachable=True)

                    if created:
                        print('Network created: %s' % created.id)


@action('stack-deploy')
class StackDeployAction(Action):
    """
    Deploy a Swarm stack (using `docker stack deploy ...`).

    First, a `debian` based image is built to execute the `docker` command in.
    Then all the `configs` and `secrets` are collected from the stack YAML.
    Their MD5 hash will be passed as environment variables to the deploy container.
    Finally, the `docker stack deploy` command is run, in the deploy container.

    To avoid having to install the whole `docker-ce` package,
      we'll just bind-mount the actual `docker` CLI binary.

    The logic for the environment variables is, for each file:
      Take the filename, convert it to upper-case, and replace any characters.
        apart from letters and digits, with underscores,
        and use this as the key for the variable.
      Examples:
        ./conf/dir/app.config        ->  APP_CONFIG
        ./secrets/ssl-cert.location  ->  SSL_CERT_LOCATION
    """

    def __init__(self, stack_name, working_dir, config_dir, volumes, stack_file='stack.yml', user=None):
        self.stack_name = stack_name
        self.working_dir = working_dir
        self.config_dir = config_dir
        self.volumes = volumes
        self.stack_file = stack_file
        self.user = user

    def _run(self):
        dockerfile = """
        FROM debian:stable-slim
        RUN apt-get update && apt-get -y install libltdl7
        """

        stack_name = self._render_with_template(self.stack_name)
        working_dir = self._render_with_template(self.working_dir)
        config_dir = self._render_with_template(self.config_dir)
        stack_file = self._render_with_template(self.stack_file)
        volumes = list(map(self._render_with_template, self.volumes))

        volumes.append('%s:%s:ro' % (working_dir, working_dir))

        client = docker.from_env()
        client.images.build(fileobj=BytesIO(dockerfile), rm=True, forcerm=True, tag='stack-deploy')

        print('Stack deploy image ready')

        secret_versions = {
            key: value
            for key, value in self._prepare_secret_versions(config_dir, stack_file)
        }

        docker_host = os.getenv('DOCKER_HOST')

        env_vars = dict(secret_versions)
        if docker_host:
            env_vars['DOCKER_HOST'] = docker_host

        print(client.containers.run(
            image='stack-deploy',
            command='docker stack deploy -c %s --resolve-image=always --with-registry-auth %s' %
                    (stack_file, stack_name),
            user=self.user, working_dir=working_dir,
            environment=env_vars,
            volumes=volumes, remove=True)
        )

        client.api.close()

    def _prepare_secret_versions(self, working_dir, stack_file):
        with open('%s/%s' % (working_dir, stack_file)) as stack_yml:
            parsed = yaml.load(stack_yml.read())

            for variable, version in self._prepare_versions_for('configs', parsed, working_dir):
                yield variable, version

            for variable, version in self._prepare_versions_for('secrets', parsed, working_dir):
                yield variable, version

    @staticmethod
    def _prepare_versions_for(root_key, parsed, working_dir):
        if root_key not in parsed:
            return

        for key, config in parsed[root_key].items():
            path = config.get('file')
            if not path:
                continue

            path = os.path.join(working_dir, path)
            if os.path.exists(path):
                with open(path, 'rb') as secret_file:
                    version = hashlib.md5(secret_file.read()).hexdigest()

                variable = os.path.basename(path).upper()
                variable, _ = re.subn('[^A-Z0-9_]', '_', variable)

                yield variable, version
