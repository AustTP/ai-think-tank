"""Container broker for agent workspaces (ask 10).

The broker hands out containers from an approved image list,
bounded by a global cap and a per-agent cap. Each agent has at
most one active container and may switch between its own
running containers. Containers can be shut down and restarted
within the caps, and provisioning may name an egress profile
the broker must recognize. All state is in memory; the broker
is the policy boundary, not the runtime.

The runtime itself is a driver seam (ContainerDriver below):
the default in-memory driver keeps the broker pure and
testable, and the Docker driver (ask 9 wiring) runs real
containers through the docker SDK. Every provision is
always-on VPN when an egress planner is configured (ask 9):
the planner picks an exit position, or records a direct
fallback when no positions exist, and the route is kept on
the container's record.
"""

import secrets
from dataclasses import dataclass
from typing import Dict, List, Optional

import docker

from egress import EgressRoute


@dataclass
class Container:
    """One provisioned container."""

    container_id: str
    agent_id: str
    image: str
    egress: Optional[str] = None
    state: str = "running"
    egress_route: Optional[EgressRoute] = None


@dataclass
class ContainerResult:
    """The outcome of a container operation."""

    ok: bool
    container_id: Optional[str] = None
    reason: str = ""


class ContainerDriver:
    """The runtime seam the broker provisions through.

    The broker is the policy boundary (caps, images,
    egress); the driver is the runtime. Tests inject an
    in-memory driver; production passes a DockerDriver,
    which talks to the docker SDK. create() returns
    (container_id, error): a None id with an error
    string fails the provision, and the broker records
    nothing."""

    def create(self, agent_id: str, image: str,
               egress_route: Optional[EgressRoute]):
        """Start a container; returns (container_id,
        error)."""
        raise NotImplementedError

    def stop(self, container_id: str) -> bool:
        """Stop a running container."""
        raise NotImplementedError

    def start(self, container_id: str) -> bool:
        """Start a stopped container."""
        raise NotImplementedError


class InMemoryDriver(ContainerDriver):
    """The default driver: no runtime at all. create()
    mints the same ctr- id the broker used to mint
    itself, so the broker stays pure and every existing
    test runs without docker."""

    def create(self, agent_id: str, image: str,
               egress_route: Optional[EgressRoute]):
        return f"ctr-{secrets.token_hex(4)}", None

    def stop(self, container_id: str) -> bool:
        return True

    def start(self, container_id: str) -> bool:
        return True


class DockerDriver(ContainerDriver):
    """The docker SDK driver. Created containers run a
    long sleep loop so the broker can exec into them
    later; a VPN route is injected as the proxy
    environment (rewritten to host.docker.internal,
    which Docker maps to the host via the extra_hosts
    host-gateway entry). The client is injectable for
    tests and lazily built from the environment in
    production."""

    SLEEP_COMMAND = ["sh", "-c", "while true; do sleep 3600; done"]

    def __init__(self, client=None):
        self._client = client

    def _connect(self):
        return self._client if self._client is not None \
            else docker.from_env()

    def create(self, agent_id: str, image: str,
               egress_route: Optional[EgressRoute]):
        environment = {}
        extra_hosts = {}
        if egress_route is not None \
                and egress_route.kind == "vpn":
            proxy = egress_route.container_proxy()
            if proxy:
                environment["HTTP_PROXY"] = proxy
                environment["HTTPS_PROXY"] = proxy
            extra_hosts = {
                "host.docker.internal": "host-gateway"
            }
        try:
            container = self._connect().containers.run(
                image,
                command=self.SLEEP_COMMAND,
                detach=True,
                environment=environment or None,
                extra_hosts=extra_hosts or None,
                labels={"agent": agent_id},
            )
        except Exception as exc:
            return None, f"docker create failed: {exc}"
        return container.id, None

    def stop(self, container_id: str) -> bool:
        try:
            self._connect().containers.get(
                container_id).stop(timeout=10)
        except Exception:
            return False
        return True

    def start(self, container_id: str) -> bool:
        try:
            self._connect().containers.get(container_id).start()
        except Exception:
            return False
        return True


class ContainerBroker:
    """Provisions, stops, restarts, and switches containers."""

    def __init__(
        self,
        max_containers: int,
        approved_images: List[str],
        max_per_agent: int = 3,
        egress_profiles: Optional[Dict[str, List[str]]] = None,
        egress_planner=None,
        driver: Optional[ContainerDriver] = None,
    ):
        """Store the caps, the approved images, and egress
        profiles. `egress_planner` (ask 9) turns every
        provision into always-on VPN when exit positions
        exist; `driver` is the runtime seam, defaulting
        to the in-memory driver."""
        self.max_containers = max_containers
        self.approved_images = set(approved_images)
        self.max_per_agent = max_per_agent
        self.egress_profiles = egress_profiles or {}
        self.egress_planner = egress_planner
        self.driver = driver if driver is not None \
            else InMemoryDriver()
        self._containers: Dict[str, Container] = {}
        self._by_agent: Dict[str, List[str]] = {}
        self._active: Dict[str, str] = {}

    def running(self) -> int:
        """Count running containers across all agents."""
        return sum(
            1
            for container in self._containers.values()
            if container.state == "running"
        )

    def running_for(self, agent_id: str) -> int:
        """Count running containers owned by one agent."""
        return sum(
            1
            for container_id in self._by_agent.get(agent_id, [])
            if self._containers[container_id].state == "running"
        )

    def provision(
        self, agent_id: str, image: str, egress: Optional[str] = None
    ) -> ContainerResult:
        """Start a new container for an agent."""
        if image not in self.approved_images:
            return ContainerResult(False, None, "image not approved")
        if egress is not None and egress not in self.egress_profiles:
            return ContainerResult(
                False, None, "unknown egress profile"
            )
        if self.running() >= self.max_containers:
            return ContainerResult(
                False, None, "global container cap reached"
            )
        if self.running_for(agent_id) >= self.max_per_agent:
            return ContainerResult(
                False, None, "per-agent container cap reached"
            )
        route = self._plan_egress()
        container_id, error = self.driver.create(
            agent_id, image, route)
        if container_id is None:
            return ContainerResult(
                False, None, error or "driver create failed"
            )
        self._containers[container_id] = Container(
            container_id=container_id,
            agent_id=agent_id,
            image=image,
            egress=egress,
            egress_route=route,
        )
        self._by_agent.setdefault(agent_id, []).append(container_id)
        self._active[agent_id] = container_id
        return ContainerResult(True, container_id, "running")

    def _plan_egress(self) -> Optional[EgressRoute]:
        """The egress route for a new container (ask 9).
        With a planner configured, egress is always-on:
        the planner picks a VPN exit when one is
        available and records the direct fallback when
        none is. Without a planner (the pure in-memory
        broker), no route is planned and `egress` stays
        a profile name."""
        if self.egress_planner is None:
            return None
        return self.egress_planner.route_for()

    def shutdown(self, agent_id: str, container_id: str) -> bool:
        """Stop one of the agent's own running containers."""
        container = self._containers.get(container_id)
        if container is None or container.agent_id != agent_id:
            return False
        if container.state == "stopped":
            return False
        if not self.driver.stop(container_id):
            return False
        container.state = "stopped"
        if self._active.get(agent_id) == container_id:
            self._active.pop(agent_id, None)
        return True

    def restart(
        self, agent_id: str, container_id: str
    ) -> ContainerResult:
        """Restart one of the agent's stopped containers."""
        container = self._containers.get(container_id)
        if container is None or container.agent_id != agent_id:
            return ContainerResult(False, None, "unknown container")
        if container.state == "running":
            return ContainerResult(
                True, container_id, "already running"
            )
        if self.running() >= self.max_containers:
            return ContainerResult(
                False, None, "global container cap reached"
            )
        if self.running_for(agent_id) >= self.max_per_agent:
            return ContainerResult(
                False, None, "per-agent container cap reached"
            )
        if not self.driver.start(container_id):
            return ContainerResult(
                False, None, "driver start failed"
            )
        container.state = "running"
        self._active[agent_id] = container_id
        return ContainerResult(True, container_id, "running")

    def switch(self, agent_id: str, container_id: str) -> bool:
        """Make one of the agent's running containers active."""
        container = self._containers.get(container_id)
        if container is None or container.agent_id != agent_id:
            return False
        if container.state != "running":
            return False
        self._active[agent_id] = container_id
        return True

    def active(self, agent_id: str) -> Optional[str]:
        """The agent's active container id, or None."""
        return self._active.get(agent_id)

    def containers_for(self, agent_id: str) -> List[str]:
        """Every container id the agent has ever provisioned."""
        return list(self._by_agent.get(agent_id, []))

    def describe(self, agent_id: str) -> List[Container]:
        """The agent's container records."""
        return [
            self._containers[container_id]
            for container_id in self._by_agent.get(agent_id, [])
        ]
