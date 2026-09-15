"""Bound Kubernetes calls and preserve resource-version and ownership fences."""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from polyad.operator.compiler.asts import GROUP as GROUP
from polyad.operator.compiler.asts import RESOURCE_TYPES, DeleteOptions, UIDPreconditions, encode_body
from polyad.operator.compiler.asts import VERSION as VERSION

KINDS = {kind: descriptor.plural for kind, descriptor in RESOURCE_TYPES.items() if descriptor.api_version == f"{GROUP}/{VERSION}"}
BUILTINS = {kind: (descriptor.prefix, descriptor.plural) for kind, descriptor in RESOURCE_TYPES.items() if kind not in KINDS}


WORKLOAD_KINDS = tuple(kind for kind in BUILTINS if kind != "Lease")


class API:
    """Use namespaced, JSON Kubernetes requests with finite transport timeouts."""

    def __init__(self, before_write: Callable[[], Awaitable[None]] | None = None) -> None:
        """Load in-cluster credentials or the developer's kubeconfig."""
        try:
            config.load_incluster_config()
        except config.ConfigException:
            config.load_kube_config()
        self.client = client.ApiClient()
        self.before_write = before_write
        self.client.rest_client.pool_manager.connection_pool_kw["retries"] = False

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
        """Execute one acknowledged request; callers retry by rereading the object."""
        prefix, plural = BUILTINS.get(kind, (f"/apis/{GROUP}/{VERSION}", KINDS.get(kind, "")))
        if not plural:
            raise ValueError(f"unsupported kind: {kind}")
        path = f"{prefix}/namespaces/{namespace}/{plural}"
        if name:
            path += f"/{name}"
        if status:
            path += "/status"
        before_write = getattr(self, "before_write", None)
        if method not in {"GET", "HEAD"} and before_write is not None:
            await before_write()
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
        """Read the latest version before deciding on a mutation."""
        return cast(dict[str, Any] | None, await self.request("GET", kind, namespace, name))

    async def owned(self, namespace: str, uid: str) -> list[dict[str, Any]]:
        """Refresh all permitted child kinds and check owner UIDs as well as labels."""
        children = []
        for kind in (*WORKLOAD_KINDS, "Graph", "EphemeralGraph", "Feedback"):
            result = await self.request("GET", kind, namespace, query=[("labelSelector", f"{GROUP}/owner={uid}")])
            for item in (result or {}).get("items", []):
                item.setdefault("kind", kind)  # Kubernetes list items may omit TypeMeta.
                if any(owner["uid"] == uid for owner in item["metadata"].get("ownerReferences", [])):
                    children.append(item)
        return children

    async def delete(self, obj: dict[str, Any]) -> None:
        """Request foreground deletion with a UID fence; later reads prove cleanup."""
        meta = obj["metadata"]
        await self.request(
            "DELETE",
            obj["kind"],
            meta["namespace"],
            meta["name"],
            DeleteOptions(preconditions=UIDPreconditions(uid=meta["uid"], resourceVersion=meta.get("resourceVersion"))),
        )
