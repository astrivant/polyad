"""
Bound Kubernetes calls and preserve resource-version and ownership fences.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from functools import cached_property
from typing import TYPE_CHECKING, cast

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from polyad.compiler.asts import GROUP as GROUP
from polyad.compiler.asts import RESOURCE_TYPES, DeleteOptions, UIDPreconditions, encode_body
from polyad.compiler.asts import VERSION as VERSION
from polyad.operator.metrics import WriteBacklog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from typing import Any

KINDS = {kind: descriptor.plural for kind, descriptor in RESOURCE_TYPES.items() if descriptor.api_version == f"{GROUP}/{VERSION}"}
BUILTINS = {kind: (descriptor.prefix, descriptor.plural) for kind, descriptor in RESOURCE_TYPES.items() if kind not in KINDS}


WORKLOAD_KINDS = tuple(kind for kind in BUILTINS if kind not in {"Lease", "Pod"})


class API:
    """
    Use namespaced, JSON Kubernetes requests with finite transport timeouts.
    """

    def __init__(self, before_write: Callable[[], Awaitable[None]] | None = None) -> None:
        """
        Load in-cluster credentials or the developer's kubeconfig.

        Args:
            before_write (Callable[[], Awaitable[None]] | None): Optional ownership check awaited before dispatching each mutation.
        """
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.writes = WriteBacklog()
        self.client = client.ApiClient()
        self.before_write = before_write
        self.client.rest_client.pool_manager.connection_pool_kw["retries"] = False

    @cached_property
    def writes(self) -> WriteBacklog:
        """
        Expose write pressure separately for each API adapter.

        Returns:
            WriteBacklog: Write backlog tracker belonging to this API adapter.
        """
        return WriteBacklog()

    @cached_property
    def write_lock(self) -> asyncio.Lock:
        """
        Preserve dispatch order for concurrent mutations through this adapter.

        Returns:
            asyncio.Lock: Event-loop lock serializing mutation dispatch.
        """
        return asyncio.Lock()

    @asynccontextmanager
    async def mutation(self, method: str) -> AsyncIterator[None]:
        """
        Track waiting writes, recheck ownership, and retain the slot through HTTP.

        Args:
            method (str): HTTP method to send to Kubernetes.

        Yields:
            None: Control while the guarded mutation or reconciliation slot is held.
        """
        if method in {"GET", "HEAD"}:
            yield
            return
        token = self.writes.enqueue()
        try:
            async with self.write_lock:
                before_write = getattr(self, "before_write", None)
                if before_write is not None:
                    await before_write()
                self.writes.dispatch(token)
                yield
        finally:
            self.writes.finish(token)

    async def request(
        self,
        method: str,
        kind: str,
        namespace: str,
        name: str = "",
        body: Any = None,
        *,
        status: bool = False,
        query: list[tuple[str, str]] | None = None,
    ) -> Any:
        """
        Execute one acknowledged request; callers retry by rereading the object.

        Args:
            method (str): HTTP method to send to Kubernetes.
            kind (str): Kubernetes resource kind.
            namespace (str): Namespace containing the operator resources.
            name (str): Resource name within its namespace.
            body (Any): Request payload, either a typed AST or native API document.
            status (bool): Whether to address the status subresource.
            query (list[tuple[str, str]] | None): Optional Kubernetes API query parameters.

        Returns:
            Any: Decoded API response, or None for an absent GET or DELETE target.
        """
        prefix, plural = BUILTINS.get(kind, (f"/apis/{GROUP}/{VERSION}", KINDS.get(kind, "")))
        if not plural:
            raise ValueError(f"unsupported kind: {kind}")
        path = f"{prefix}/namespaces/{namespace}/{plural}"
        if name:
            path += f"/{name}"
        if status:
            path += "/status"
        async with self.mutation(method):
            try:
                request = asyncio.create_task(
                    asyncio.to_thread(
                        self.client.call_api,
                        path,
                        method,
                        body=encode_body(body),
                        query_params=query or [],
                        response_type="object",
                        auth_settings=["BearerToken"],
                        header_params={"Content-Type": "application/merge-patch+json" if method == "PATCH" else "application/json"},
                        _return_http_data_only=True,
                        _request_timeout=(5, 20),
                    )
                )
                try:
                    return await asyncio.shield(request)
                except asyncio.CancelledError:
                    # Cancelling to_thread does not stop HTTP. Join it before ownership ends.
                    await asyncio.gather(request, return_exceptions=True)
                    raise
            except ApiException as error:
                if error.status == 404 and method in {"GET", "DELETE"}:
                    return None
                raise

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
        return cast("dict[str, Any] | None", await self.request("GET", kind, namespace, name))

    async def owned(self, namespace: str, uid: str) -> list[dict[str, Any]]:
        """
        Refresh all permitted child kinds and check owner UIDs as well as labels.

        Args:
            namespace (str): Namespace containing the operator resources.
            uid (str): Persisted Kubernetes identity used to fence ownership.

        Returns:
            list[dict[str, Any]]: Children whose owner references match the requested UID.
        """
        children = []
        for kind in (*WORKLOAD_KINDS, "Graph", "EphemeralGraph", "Feedback", "PolyGraph"):
            result = await self.request("GET", kind, namespace, query=[("labelSelector", f"{GROUP}/owner={uid}")])
            for item in (result or {}).get("items", []):
                item.setdefault("kind", kind)  # Kubernetes list items may omit TypeMeta.
                if any(owner["uid"] == uid for owner in item["metadata"].get("ownerReferences", [])):
                    children.append(item)
        return children

    async def delete(self, obj: dict[str, Any]) -> None:
        """
        Request foreground deletion with a UID fence; later reads prove cleanup.

        Args:
            obj (dict[str, Any]): Resource document from the latest API observation.

        Returns:
            None: No return value.
        """
        meta = obj["metadata"]
        await self.request(
            "DELETE",
            obj["kind"],
            meta["namespace"],
            meta["name"],
            DeleteOptions(preconditions=UIDPreconditions(uid=meta["uid"], resourceVersion=meta.get("resourceVersion"))),
        )
