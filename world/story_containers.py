"""Persistent, resource-bounded Docker environments isolated by story workspace."""
import hashlib
import json
from pathlib import Path
import subprocess
import threading

_GUARD = threading.Lock()
_LOCKS = {}


def identity(directory):
    path = Path(directory).resolve()
    owner = path.name
    metadata = path.parent / (path.name + '.runtime.json')
    if metadata.is_file():
        value = json.loads(metadata.read_text(encoding='utf-8')).get('workspace')
        if isinstance(value, str) and value.startswith('story-') and '/' not in value and '\\' not in value:
            owner = value
    name = 'think-story-' + hashlib.sha256(str(path).encode()).hexdigest()[:20]
    volume = 'think-deps-' + hashlib.sha256(str(path.parent / owner).encode()).hexdigest()[:20]
    return path, name, volume


def lock(directory):
    _, name, _ = identity(directory)
    with _GUARD:
        return _LOCKS.setdefault(name, threading.RLock())


def _run(command, timeout=30):
    return subprocess.run(command, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)


def ensure(directory, serve):
    path, name, volume = identity(directory)
    with lock(directory):
        probe = _run(['docker', 'inspect', name])
        if probe.returncode:
            runtime_image = 'think-story-env:' + volume.removeprefix('think-deps-')
            image_probe = _run(['docker', 'image', 'inspect', runtime_image])
            image = runtime_image if image_probe.returncode == 0 else serve.SANDBOX_IMAGE
            proxy = f'http://{serve.PROXY_CONTAINER}:{serve.PROXY_PORT}'
            command = ['docker', 'create', '--name', name, '--label', 'ai-think-tank.managed=story',
                '--network', serve.SANDBOX_NETWORK, '--memory', '512m', '--cpus', '1', '--pids-limit', '128',
                '--mount', f'type=bind,source={path},target=/workspace',
                '--mount', f'type=volume,source={volume},target=/opt/story-deps', '-w', '/workspace']
            for value in (f'http_proxy={proxy}', f'https_proxy={proxy}', f'HTTP_PROXY={proxy}', f'HTTPS_PROXY={proxy}',
                          'PIP_TARGET=/opt/story-deps/python', 'PYTHONPATH=/opt/story-deps/python',
                          'PATH=/opt/story-deps/python/bin:/opt/story-deps/bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin',
                          'npm_config_prefix=/opt/story-deps', 'OPENBLAS_NUM_THREADS=1', 'OMP_NUM_THREADS=1'):
                command += ['-e', value]
            created = _run(command + [image, 'sleep', 'infinity'])
            if created.returncode:
                raise RuntimeError('Could not create story container: ' + created.stderr[-1000:])
        else:
            config = json.loads(probe.stdout)[0]
            mounts = config.get('Mounts') or []
            if ((config.get('Config', {}).get('Labels') or {}).get('ai-think-tank.managed') != 'story'
                    or not any(m.get('Destination') == '/workspace' and m.get('Type') == 'bind' for m in mounts)):
                raise RuntimeError('Story container has an unexpected workspace mount.')
        started = _run(['docker', 'start', name])
        if started.returncode:
            raise RuntimeError('Could not start story container: ' + started.stderr[-1000:])
    return name


def execute(directory, command, serve, soft_limit=True):
    with lock(directory):
        name = ensure(directory, serve)
        try:
            result = _run(['docker', 'exec', '-w', '/workspace', name, 'sh', '-c', command],
                          timeout=600 if soft_limit else serve.SANDBOX_TIMEOUT_S)
            return {'exitCode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr, 'timedOut': False}
        except subprocess.TimeoutExpired as error:
            # Kill this container's process tree; the next command restarts its environment.
            _run(['docker', 'stop', '-t', '1', name])
            return {'exitCode': None, 'stdout': serve._sandbox_output_text(error.stdout),
                    'stderr': serve._sandbox_output_text(error.stderr), 'timedOut': True}


def fingerprint(directory, serve):
    with lock(directory):
        name = ensure(directory, serve)
        env = _run(['docker', 'exec', name, 'sh', '-c',
                    'python -m pip freeze && dpkg-query -W'], timeout=15)
        image = _run(['docker', 'inspect', '--format', '{{.Image}}', name])
        if env.returncode or image.returncode:
            return None
        return hashlib.sha256((image.stdout + env.stdout).encode()).hexdigest()


def enabled(directory):
    path, _, _ = identity(directory)
    return path.name.startswith('story-') or (path.parent / (path.name + '.runtime.json')).is_file()


def release(directory):
    """Drop only this managed container before its bind directory is replaced."""
    with lock(directory):
        _, name, _ = identity(directory)
        probe = _run(['docker', 'inspect', name])
        if probe.returncode:
            return
        labels = (json.loads(probe.stdout)[0].get('Config', {}).get('Labels') or {})
        if labels.get('ai-think-tank.managed') != 'story':
            raise ValueError('Refusing to remove an unmanaged container.')
        result = _run(['docker', 'rm', '-f', name])
        if result.returncode:
            raise RuntimeError('Could not release the story workspace container: ' + result.stderr[-500:])


def checkpoint_environment(directory):
    """Preserve OS packages too; Docker commits exclude bind-mounted source/data."""
    with lock(directory):
        _, name, volume = identity(directory)
        probe = _run(['docker', 'inspect', name])
        if probe.returncode:
            return
        if ((json.loads(probe.stdout)[0].get('Config') or {}).get('Labels') or {}).get('ai-think-tank.managed') != 'story':
            raise ValueError('Cannot snapshot an unmanaged environment.')
        image = 'think-story-env:' + volume.removeprefix('think-deps-')
        result = _run(['docker', 'commit', '--change', 'LABEL ai-think-tank.managed=story-env', name, image], timeout=120)
        if result.returncode:
            raise RuntimeError('Could not checkpoint story dependencies: ' + result.stderr[-500:])
