"""Always-on VPN egress for containers (ask 9).

Every container the broker provisions should leave the
host through a Mullvad exit, not through the host's own
route: an agent's fetch should not be traceable to the
think tank's address, and two agents browsing at once
should not contend for one host-wide tunnel. This module
plans that egress. Given the registry of usable exit
positions (serve's _vpn_exit_registry, which merges the
static exits with whatever the operator started through
the on-demand pool), it hands out one exit per container,
round-robin over the sorted names so the choice is
stable and testable.

The rule is always-on unless there is nothing to route
through: when the registry is empty the planner returns
a direct route that records why, so a container still
runs (its record carries the reason) instead of the
provision failing. The planner never starts or stops
exits -- spawning an exit container burns one of the
account's five Mullvad devices, so that stays with the
operator-gated pool (vpn_pool.py).
"""

import time
import urllib.parse
from dataclasses import dataclass
from typing import Optional

VPN = 'vpn'
DIRECT = 'direct'

FALLBACK_REASON = 'no vpn exit positions available'


@dataclass
class EgressRoute:
    """One container's egress decision: whether it leaves
    through a VPN exit or directly, and the exit it used
    (or the reason it fell back). `proxy` is the exit's
    proxy URL as recorded in the registry -- on the host's
    loopback. A container inside docker cannot reach the
    host's loopback, so container_proxy() rewrites it for
    use from inside a container. A dataclass so a broker
    Container's record serializes through
    dataclasses.asdict unchanged: the container list
    endpoint hands whole records to JSON, and a plain
    class here would fail that serialization."""

    kind: str
    exit_name: Optional[str] = None
    proxy: Optional[str] = None
    proxy_port: Optional[int] = None
    relay: Optional[str] = None
    reason: str = ''

    def container_proxy(self):
        """The proxy URL as a container should reach it.
        The registry records the proxy on the host loopback
        (127.0.0.1), which inside a container is the
        container itself -- so the host is rewritten to
        host.docker.internal, which Docker maps back to
        the host when the broker passes it as an
        extra_hosts host-gateway entry."""
        if self.kind != VPN or not self.proxy:
            return None
        parsed = urllib.parse.urlsplit(self.proxy)
        port = parsed.port
        netloc = ('host.docker.internal:%d' % port
                  if port else 'host.docker.internal')
        return urllib.parse.urlunsplit(
            (parsed.scheme, netloc, parsed.path,
             parsed.query, parsed.fragment))


class ContainerEgress(object):
    """The always-on egress planner. `positions_callable`
    returns the exit registry (name -> metadata with at
    least a `proxy` URL); it is called per planning so a
    stopped exit is never handed out. The clock is
    injectable for tests, though the planner itself is
    stateless apart from the round-robin cursor."""

    def __init__(self, positions_callable, clock=None):
        self._positions = positions_callable
        self.clock = clock or time.time
        self._cursor = 0

    def route_for(self):
        """Pick the egress route for one new container.
        VPN through the next exit position (round-robin
        over the sorted names, so the rotation is stable
        and reproducible), or direct when the registry is
        empty -- always-on, with the fallback recorded
        rather than hidden."""
        positions = self._positions() or {}
        names = sorted(positions)
        if not names:
            return EgressRoute(DIRECT, reason=FALLBACK_REASON)
        name = names[self._cursor % len(names)]
        self._cursor += 1
        meta = positions.get(name) or {}
        proxy = meta.get('proxy')
        proxy_port = meta.get('proxyPort')
        if proxy_port is None and proxy:
            proxy_port = urllib.parse.urlsplit(proxy).port
        return EgressRoute(
            VPN,
            exit_name=name,
            proxy=proxy,
            proxy_port=proxy_port,
            relay=meta.get('relay'),
            reason='via exit %s' % name)
