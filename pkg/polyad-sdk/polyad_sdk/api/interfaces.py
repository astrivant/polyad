"""
Define separately authorized feedback and connection actions for applications.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typing import Any

    from polyad_types import AdaptationReport, ConnectionResponse, ServiceConnectionRequest, ServiceLevelReport, ThroughputSample


class ServiceLevelReporter(ABC):
    """
    Publish service observations independently of adaptation lifecycle events.
    """

    @abstractmethod
    def report_service_level(self, report: ServiceLevelReport) -> dict[str, Any]:
        """
        Persist one fenced, non-overlapping SLA observation window.

        Args:
            report (ServiceLevelReport): Request, quality, capability and availability observations.

        Returns:
            dict[str, Any]: Current evaluated service-level status.
        """
        ...


class AdaptationReporter(ABC):
    """
    Publish strategy lifecycle so definition health reflects application adaptation.
    """

    @abstractmethod
    def report_adaptation(self, report: AdaptationReport) -> dict[str, Any]:
        """
        Persist the start or terminal state of one strategy invocation.

        Args:
            report (AdaptationReport): Fenced definition and invocation transition.

        Returns:
            dict[str, Any]: Current definition adaptation acknowledgement.
        """
        ...


class ThroughputReporter(ABC):
    """
    Submit application demand through its separately authorized transport.
    """

    @abstractmethod
    def report_throughput(self, sample: ThroughputSample) -> dict[str, Any]:
        """
        Report aggregate demand and completed work for the currently observed graph revision.

        Args:
            sample (ThroughputSample): Fresh measurement using the graph policy's work unit.

        Returns:
            dict[str, Any]: Operator acknowledgement; layout changes are asynchronous.
        """
        ...


class ConnectionNegotiator(ABC):
    """
    Request and answer connections through a separately authorized transport.
    """

    @abstractmethod
    def connect_services(self, request: ServiceConnectionRequest) -> dict[str, Any]:
        """
        Propose a TTL connection between discovered services through their common boundary owner.

        Args:
            request (ServiceConnectionRequest): Exact service identities and stable idempotency key.

        Returns:
            dict[str, Any]: Durable proposal awaiting peer approval and policy admission.
        """
        ...

    @abstractmethod
    def respond_connection(self, namespace: str, name: str, response: ConnectionResponse) -> dict[str, Any]:
        """
        Approve or reject an event's proposal using this endpoint's projected Pod token.

        Args:
            namespace (str): Receipt namespace from the connection event.
            name (str): Server-assigned receipt name from the event.
            response (ConnectionResponse): Receipt UID and explicit approval or rejection.

        Returns:
            dict[str, Any]: Durable consent receipt; activation remains asynchronous.
        """
        ...
