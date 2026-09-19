"""
Define authorized topology reads, replayable observations and endpoint discovery.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from threading import Event as StopEvent
    from typing import Any, Literal

    from polyad_sdk.events.subscriptions import Subscription
    from polyad_types.events.envelope import Event


class EventSource(ABC):
    """
    Supply topology, replayable events and trusted endpoint discovery.

    Implementations preserve graph identity, authorization, bounded reads and
    cancellation. subscribe() supplies the shared hook/checkpoint lifecycle.

    Attributes:
        url (str): Stable configured authority used to scope idempotency keys.
    """

    url: str

    @abstractmethod
    def topology(
        self, *, graph: str, kind: str = "Graph", graph_uid: str | None = None, node: str | None = None, cluster: str | None = None
    ) -> dict[str, Any]:
        """
        Read neighbors and a starting cursor using the events Service and its credential.

        Args:
            graph (str): Persisted graph instance name.
            kind (str): Graph, PolyGraph or ReplicaGroup.
            graph_uid (str | None): Expected graph incarnation; replacements return HTTP 409.
            node (str | None): Logical node to inspect; omitted returns the entire boundary.
            cluster (str | None): Registered cluster when reading through a root control plane.

        Returns:
            dict[str, Any]: Current topology or neighbors, including revision and replay cursor.
        """
        ...

    @abstractmethod
    def events(
        self,
        *,
        last_event_id: str | None = None,
        cluster: str | None = None,
        transport: Literal["sse", "websocket"] = "sse",
        endpoint: tuple[str, int] | None = None,
        stop_event: StopEvent | None = None,
        heartbeats: bool = False,
    ) -> Iterator[Event]:
        """
        Stream observations using a client configured for the separate events Service.

        Args:
            last_event_id (str | None): Last processed cursor for explicit reconnection.
            cluster (str | None): Registered cluster stream; cursors belong to that selected stream.
            transport (Literal['sse', 'websocket']): Subscription framing; WebSocket requires operator enablement.
            endpoint (tuple[str, int] | None): Explicit trusted socket target; normally selected by a rebalancing subscription.
            stop_event (StopEvent | None): Optional cancellation, checked on heartbeats and observations.
            heartbeats (bool): Also emit empty SSE heartbeat observations for application refresh scheduling.

        Returns:
            Iterator[Event]: Bounded observations and stream controls in replay order.
        """
        ...

    @abstractmethod
    def event_endpoints(self, *, cluster: str | None = None) -> dict[str, Any]:
        """
        Discover stream replicas from the configured authority, never from an event-provided URL.

        Args:
            cluster (str | None): Root-held cluster stream; authorization must match the subscription.

        Returns:
            dict[str, Any]: Operator membership, routing policy and initial replay cursor.
        """
        ...

    def subscribe(
        self,
        *,
        cluster: str | None = None,
        cursor: str | None = None,
        history: int = 1024,
        transport: Literal["sse", "websocket"] = "sse",
        rebalance: bool = False,
        heartbeats: bool = False,
    ) -> Subscription:
        """
        Build a resumable subscription with explicit application event hooks.

        Args:
            cluster (str | None): Registered cluster stream.
            cursor (str | None): Previously committed stream cursor.
            history (int): Bounded count of successful event-handler calls remembered during retries.
            transport (Literal['sse', 'websocket']): SSE by default, or an operator-enabled WebSocket subscription.
            rebalance (bool): Rediscover and reconnect on copulses or transient transport failures, preserving completed checkpoints.
            heartbeats (bool): Deliver SSE heartbeats to hooks that maintain periodic application observations.

        Returns:
            Subscription: Register filters and callbacks, then call run on the application's chosen thread.
        """
        from polyad_sdk.events.subscriptions import Subscription

        return Subscription(
            self, cluster=cluster, cursor=cursor, history=history, transport=transport, rebalance=rebalance, heartbeats=heartbeats
        )
