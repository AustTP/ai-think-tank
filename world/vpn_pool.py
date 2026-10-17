"""On-demand VPN exits for any container (ask 13).

The vpn-sandbox image (sandboxes/vpn-sandbox) brings up one
Mullvad WireGuard tunnel inside a container and publishes a
tinyproxy on a host port. Until now that image only ran as the
two hand-built exits wired through VPN_EXIT_PROXIES and
sandboxes/vpn-sandbox/exits.json (us-nyc, us-atl): a VPN
vantage point existed only inside that dedicated sandbox. This
module spawns the same exit container on demand for any Mullvad
relay, so any agent or container can get its own exit. The pool
allocates a host port, runs the image with the relay hostname,
and hands back a registry record shaped like the static exits, so
browse_page's `exit` parameter, POST /api/browse's viaVpnExit,
and GET /api/vpn-exits keep working unchanged.

Mullvad accounts allow 5 devices, and each running exit registers
one device inside its container (entrypoint.sh persists the
keypair in a per-exit volume, so a restarted exit reuses its
slot). The pool therefore caps live exits at 5 by default and
refuses to start a sixth instead of silently burning account
devices. Ports the pool must never hand out (the static exits'
published ports) are reserved up front.

The docker calls are injectable: tests pass fakes and assert on
the argv; the defaults shell out to the docker CLI.
"""
import re
import subprocess  # nosec B404 -- docker CLI only, fixed argv lists, never a shell string
from dataclasses import dataclass
from typing import Callable, Optional

DEFAULT_IMAGE = 'vpn-sandbox'
DEFAULT_FIRST_PORT = 18803  # 18801/18802 belong to the static exits
DEFAULT_MAX_EXITS = 5       # Mullvad devices per account
CONTAINER_PROXY_PORT = 8888
RUN_TIMEOUT_S = 60
RM_TIMEOUT_S = 30

# Docker container names: alphanumeric start, then letters,
# digits, underscores, dots, hyphens.
_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')


@dataclass
class VpnExit:
    """One running exit container and the host port it publishes."""
    name: str
    relay: str
    proxy: str
    proxy_port: int
    container_id: str = ''
    state: str = 'running'


def _docker_run(argv):
    """Run a docker command, returning the new container id or
    None on any failure."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True,  # nosec B603 -- fixed argv list built by this module, never a shell string
                                timeout=RUN_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _docker_rm(argv):
    """Force-remove a container; True only on a clean exit."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True,  # nosec B603 -- fixed argv list built by this module, never a shell string
                                timeout=RM_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


class VpnExitPool:
    """Spawns and retires vpn-sandbox exit containers on demand.

    The account number is the durable Mullvad credential (see
    library/skills/mullvad.md); it is passed to the container,
    never logged or stored in the registry records.
    """

    def __init__(
        self,
        account_number,
        image=DEFAULT_IMAGE,
        first_port=DEFAULT_FIRST_PORT,
        max_exits=DEFAULT_MAX_EXITS,
        reserved_ports=(),
        docker_run: Optional[Callable[[list], Optional[str]]] = _docker_run,
        docker_rm: Optional[Callable[[list], bool]] = _docker_rm,
    ):
        self.account = account_number
        self.image = image
        self.max_exits = max_exits
        self.docker_run = docker_run
        self.docker_rm = docker_rm
        self._exits: dict = {}
        self._first_port = first_port
        # Ports the pool may never hand out: the static exits'
        # published ports (reserved) plus every port it allocated
        # and has not freed yet.
        self._ports: set = set(reserved_ports)

    def start(self, relay, name=None):
        """Start an exit container for one Mullvad relay. Returns
        (exit, None) on success, or (None, reason)."""
        relay = (relay or '').strip()
        if not relay:
            return None, 'relay required'
        if not self.account:
            return None, 'Mullvad account required'
        name = (name or relay).strip()
        if not _NAME_RE.match(name):
            return None, f'invalid exit name "{name}"'
        if name in self._exits:
            return None, f'exit "{name}" already running'
        if self.running() >= self.max_exits:
            return None, (f'device cap reached ({self.max_exits} '
                          'Mullvad devices per account)')
        port = self._allocate_port()
        argv = [
            'docker', 'run', '-d', '--name', name, '--privileged',
            '-e', f'MULLVAD_ACCOUNT={self.account}',
            '-e', f'RELAY_HOSTNAME={relay}',
            '-p', f'{port}:{CONTAINER_PROXY_PORT}',
            '-v', f'vpn-exit-{name}:/etc/wireguard',
            self.image,
        ]
        container_id = self.docker_run(argv)
        if not container_id:
            return None, f'docker run failed for exit "{name}"'
        self._ports.add(port)
        exit_record = VpnExit(
            name=name,
            relay=relay,
            proxy=f'http://127.0.0.1:{port}',
            proxy_port=port,
            container_id=container_id,
        )
        self._exits[name] = exit_record
        return exit_record, None

    def stop(self, name):
        """Retire one exit. True only when the container was
        removed and its port freed."""
        exit_record = self._exits.get(name)
        if exit_record is None:
            return False
        if not self.docker_rm(['docker', 'rm', '-f', name]):
            return False
        self._ports.discard(exit_record.proxy_port)
        del self._exits[name]
        return True

    def stop_all(self):
        """Retire every exit; returns how many were removed."""
        stopped = 0
        for name in list(self._exits):
            if self.stop(name):
                stopped += 1
        return stopped

    def get(self, name):
        return self._exits.get(name)

    def names(self):
        return list(self._exits)

    def running(self):
        return len(self._exits)

    def exits(self):
        """Registry records shaped like the static exits, so
        vpn_location.describe_exit and refresh_exit accept them
        unchanged."""
        return {
            name: {
                'proxy': exit_record.proxy,
                'relay': exit_record.relay,
                'proxyPort': exit_record.proxy_port,
                'useFor': (f'on-demand exit through Mullvad relay '
                           f'{exit_record.relay}'),
            }
            for name, exit_record in self._exits.items()
        }

    def _allocate_port(self):
        port = self._first_port
        while port in self._ports:
            port += 1
        return port
