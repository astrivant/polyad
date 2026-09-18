"""
Expose operator resource and durable-state contracts without loading their drivers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable
    from typing import Any


class ResourceAPI(ABC):
    """
    Read resources and issue version-fenced mutations through a bounded adapter.
    """

    @abstractmethod
    def request(
        self,
        method: str,
        kind: str,
        namespace: str,
        name: str = "",
        body: Any = None,
        *,
        status: bool = False,
        query: list[tuple[str, str]] | None = None,
    ) -> Awaitable[Any]:
        """
        Issue a bounded read or guarded mutation, surfacing conflicts to the caller.

        Args:
            method (str): Kubernetes HTTP method.
            kind (str): Resource kind.
            namespace (str): Target namespace.
            name (str): Named resource, empty for creation or collection reads.
            body (Any): Native or typed Kubernetes document.
            status (bool): Whether to write the status subresource.
            query (list[tuple[str, str]] | None): Query parameters including dry-run selection.

        Returns:
            Awaitable[Any]: Response document after transport and admission complete.
        """
        ...

    @abstractmethod
    async def get(self, kind: str, namespace: str, name: str) -> dict[str, Any] | None:
        """
        Read the latest version before deciding on a mutation.

        Args:
            kind (str): Kubernetes resource kind.
            namespace (str): Namespace containing the operator resources.
            name (str): Resource name within its namespace.

        Returns:
            dict[str, Any] | None: Latest resource document, or None if it no longer exists.
        """
        ...

    @abstractmethod
    async def owned(self, namespace: str, uid: str) -> list[dict[str, Any]]:
        """
        Refresh all permitted child kinds and check owner UIDs as well as labels.

        Args:
            namespace (str): Namespace containing the operator resources.
            uid (str): Persisted Kubernetes identity used to fence ownership.

        Returns:
            list[dict[str, Any]]: Children whose owner references match the requested UID.
        """
        ...

    @abstractmethod
    async def delete(self, obj: dict[str, Any]) -> None:
        """
        Request foreground deletion with a UID fence; later reads prove cleanup.

        Args:
            obj (dict[str, Any]): Resource document from the latest API observation.

        Returns:
            None: No return value.
        """
        ...


class StateBackend(ABC):
    """
    Persist complete graph scans and approved events with stale-scan rejection.
    """

    @abstractmethod
    async def start(self) -> None:
        """
        Initialize shared storage safely across concurrent operator replicas.

        Returns:
            None: Failed initialization can be retried on the next observation.
        """
        ...

    @abstractmethod
    async def begin(self) -> Any:
        """
        Obtain a shared ordering ticket before reading Kubernetes for a complete scan.

        Returns:
            Any: Backend-owned ticket used to reject scans older than a committed scan.
        """
        ...

    @abstractmethod
    async def save(self, cluster: str, namespace: str, started: Any, objects: list[dict[str, Any]], snapshot: dict[str, Any]) -> bool:
        """
        Replace one complete namespace observation, including removals and tracked parameters.

        Args:
            cluster (str): Registered cluster identity; local deployments use local.
            namespace (str): Namespace scanned in that cluster.
            started (Any): Backend-owned ordering ticket acquired before the scan.
            objects (list[dict[str, Any]]): All objects from a successful complete scan.
            snapshot (dict[str, Any]): Inventory and queue observations, including freshness fields.

        Returns:
            bool: Whether this observation replaced the previous complete scan.
        """
        ...

    @abstractmethod
    async def record_event(self, payload: dict[str, Any]) -> None:
        """
        Archive approved observations with deduplication and bounded retention cleanup.

        Args:
            payload (dict[str, Any]): Public observation or topology event, excluding workload manifests and Secrets.

        Returns:
            None: Committed before live delivery; failed archive writes are retried during reconciliation.
        """
        ...

    @abstractmethod
    async def connections(self) -> dict[str, Any]:
        """
        Report connection pressure and state freshness for operator telemetry.

        Returns:
            dict[str, Any]: Global connection count, or an explicitly unavailable sample.
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """
        Release owned storage connections without deleting durable state.

        Returns:
            None: No return value.
        """
        ...
