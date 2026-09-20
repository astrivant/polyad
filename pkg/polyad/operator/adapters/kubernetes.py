"""
Bound Kubernetes calls and preserve resource-version and ownership fences.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import time
from contextlib import asynccontextmanager
from functools import cached_property
from typing import TYPE_CHECKING, cast

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
from opentelemetry import trace

from polyad.compiler.registry import GRAPH_OWNED_KINDS, RESOURCE_TYPES
from polyad.operator.adapters.interfaces import ResourceAPI
from polyad.operator.coordination.contracts import active_contract, without_capture
from polyad.operator.coordination.dispatch import DispatchGraph, admission
from polyad.operator.coordination.settings import WorkGraphSettings
from polyad.operator.coordination.validation import ValidationQueue, invalidate
from polyad.operator.coordination.write_queue import PendingWrites, WriteConflict, write_intent
from polyad.operator.observability.decisions import decision
from polyad.operator.observability.metrics import WriteBacklog
from polyad.operator.observability.tracing import traced
from polyad.transport.settings import settings
from polyad_types.resources import GROUP as GROUP
from polyad_types.resources import VERSION as VERSION
from polyad_types.resources import DeleteOptions, UIDPreconditions, encode_body

__all__ = (
    "API",
    "BUILTINS",
    "GROUP",
    "KINDS",
    "VERSION",
    "WORKLOAD_KINDS",
)


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from typing import Any

    from polyad.operator.coordination.write_queue import WriteIntent

KINDS = {kind: descriptor.plural for kind, descriptor in RESOURCE_TYPES.items() if descriptor.polyad}
BUILTINS = {kind: (descriptor.prefix, descriptor.plural) for kind, descriptor in RESOURCE_TYPES.items() if kind not in KINDS}


WORKLOAD_KINDS = tuple(kind for kind in BUILTINS if kind in GRAPH_OWNED_KINDS)


class API(ResourceAPI):
    """
    Use namespaced, JSON Kubernetes requests with finite transport timeouts.

    Attributes:
        on_write_drift (Callable[[tuple[str, str, str]], Awaitable[None]] | None): Optional publisher of coalesced recovery keys.
    """

    on_write_drift: Callable[[tuple[str, str, str]], Awaitable[None]] | None = None

    def __init__(
        self,
        before_write: Callable[[], Awaitable[None]] | None = None,
        *,
        configuration: client.Configuration | None = None,
        cluster: str | None = None,
    ) -> None:
        """
        Load in-cluster credentials or the developer's kubeconfig.

        Args:
            before_write (Callable[[], Awaitable[None]] | None): Optional ownership check awaited before dispatching each mutation.
            configuration (client.Configuration | None): Isolated remote credentials; omitted uses the local cluster.
            cluster (str | None): Registry identity for decision logs; omitted selects the local cluster setting.
        """
        if configuration is None:
            try:
                config.load_incluster_config()
            except config.ConfigException:
                config.load_kube_config()
        configuration = copy.deepcopy(configuration) if configuration is not None else client.Configuration.get_default_copy()
        configuration.connection_pool_maxsize = self.connection_settings["poolSize"]
        self.writes = WriteBacklog()
        self.client = client.ApiClient(configuration=configuration)
        self.before_write = before_write
        self.cluster = cluster if cluster is not None else os.environ.get("POLYAD_CLUSTER_NAME", "")
        self.client.rest_client.pool_manager.connection_pool_kw["retries"] = False
        _ = self.max_pending_writes  # Validate the process setting at startup.
        _ = self.write_lock
        _ = self.validations.settings

    @cached_property
    def connection_settings(self) -> dict[str, Any]:
        """
        Retain finite request timeouts and the HTTP keepalive pool size.

        Returns:
            dict[str, Any]: Validated transport settings, separate from write admission.
        """
        return settings("kubernetes")

    @cached_property
    def work_graph(self) -> WorkGraphSettings:
        """
        Retain validated process limits for this adapter's worker and admission budgets.

        Returns:
            WorkGraphSettings: Settings loaded before this adapter starts accepting writes.
        """
        return WorkGraphSettings.from_environment()

    @cached_property
    def max_pending_writes(self) -> int:
        """
        Bound admitted waiters separately from active validation or transport slots.

        Returns:
            int: Configured burst allowance; rejected callers must retry from fresh intent.
        """
        return self.work_graph.max_pending

    @cached_property
    def writes(self) -> WriteBacklog:
        """
        Expose write pressure separately for each API adapter.

        Returns:
            WriteBacklog: Write backlog tracker belonging to this API adapter.
        """
        return WriteBacklog()

    @cached_property
    def write_lock(self) -> DispatchGraph:
        """
        Order dependent writes and admit only proven independent planner siblings together.

        Returns:
            DispatchGraph: Bounded dependency scheduler on the operator event loop.
        """
        return DispatchGraph(self.work_graph.max_in_flight)

    @cached_property
    def validations(self) -> ValidationQueue:
        """
        Retain one bounded async validator producer per cluster adapter.

        Returns:
            ValidationQueue: Validation receipts consumed by ready writers.
        """
        return ValidationQueue(settings=self.work_graph.validation)

    @cached_property
    def active_write_targets(self) -> set[tuple[str, str, str]]:
        """
        Prevent cached dependency checks from overlapping an uncertain write.

        Returns:
            set[tuple[str, str, str]]: Named targets or a wildcard for an unidentifiable effect, held through transport completion.
        """
        return set()

    @cached_property
    def pending_calls(self) -> dict[object, asyncio.Future[Any]]:
        """
        Share results only while an equivalent write and dependency contract are pending.

        Returns:
            dict[object, asyncio.Future[Any]]: Bounded admitted calls; responses are discarded on completion.
        """
        return {}

    @asynccontextmanager
    async def mutation(self, method: str, intent: WriteIntent | None = None) -> AsyncIterator[None]:
        """
        Track waiting writes, recheck ownership, and retain the slot through HTTP.

        Args:
            method (str): HTTP method to send to Kubernetes.
            intent (WriteIntent | None): Comparable snapshot of a named persistent write.

        Yields:
            None: Control while the guarded mutation or reconciliation slot is held.
        """
        if method in {"GET", "HEAD"}:
            yield
            return
        token = None
        validation = None
        contract = active_contract.get()
        try:
            if self.writes.snapshot()["total"] >= self.max_pending_writes + self.write_lock.limit:
                raise WriteConflict("write_queue_full", status=429)
            token = self.writes.enqueue()
            if intent is not None:
                self.pending_writes.add(token, intent)
            waited = self.write_lock.locked() or self.writes.snapshot()["queued"] > 1
            validation = self.validations.add(token, self, intent, contract, waited)
            for conflict in self.pending_writes.conflicted:
                if conflict in self.validations.pending:
                    self.validations.pending[conflict].error = WriteConflict("overlapping_pending_writes")
            async with self.write_lock.hold(token, validation):
                self.pending_writes.check(token)
                await validation.ensure()
                before_write = getattr(self, "before_write", None)
                if before_write is not None:
                    with without_capture():
                        await before_write()

                # Another writer can arrive during validation or lease checks.
                self.pending_writes.check(token)
                if not validation.fresh():
                    raise WriteConflict("validation_expired_during_authorization")
                self.pending_writes.remove(token)
                await self.validations.remove(token)
                target = intent.target if intent is not None else ("*", "", "")
                self.active_write_targets.add(target)
                invalidate(self, target)
                self.writes.dispatch(token)
                try:
                    yield
                finally:
                    self.active_write_targets.discard(target)
                    invalidate(self, target)
        except WriteConflict as error:
            decision(
                "polyad.kubernetes.write_deferred",
                "A queued Kubernetes change conflicts with pending intent or current state; reconcile fresh desired state before retrying.",
                key=intent.target if intent is not None else None,
                outcome="deferred",
                reason=error.conflict_reason,
                level=logging.WARNING,
                attributes={
                    "http.request.method": method,
                    "http.response.status_code": error.status,
                    "polyad.target.cluster": getattr(self, "cluster", os.environ.get("POLYAD_CLUSTER_NAME", "")),
                },
            )
            if contract is not None:
                if validation is not None and validation.contract is not None:
                    for adapter, keys in validation.contract.affected.items():
                        contract.affected.setdefault(adapter, set()).update(keys)
                await contract.refresh()
            raise
        finally:
            if token is not None:
                self.pending_writes.remove(token)
                try:
                    await self.validations.remove(token)
                finally:
                    self.writes.finish(token)

    @cached_property
    def pending_writes(self) -> PendingWrites:
        """
        Track comparable pending intents independently for this cluster adapter.

        Returns:
            PendingWrites: In-memory conflict registry with no request bodies or credentials.
        """
        return PendingWrites()

    async def revalidate_write(self, intent: WriteIntent) -> None:
        """
        Recheck the target after queue delay without rebasing an old decision onto a new revision.

        Args:
            intent (WriteIntent): Original named request and its immutable identity fences.

        Returns:
            None: Stale or unfenced delayed writes raise a retryable conflict before transport.
        """
        if intent.method in {"PUT", "PATCH"} and intent.revision is None:
            raise WriteConflict("queued_write_missing_revision")
        if intent.method == "DELETE" and intent.uid is None and intent.revision is None:
            raise WriteConflict("queued_delete_missing_precondition")
        current = await self.get(*intent.target)
        if intent.method == "POST":
            if current is not None:
                raise WriteConflict("queued_create_target_exists")
            return
        if current is None:
            if intent.method == "DELETE":
                return
            raise WriteConflict("queued_write_target_absent")
        metadata = current.get("metadata", {})
        if intent.uid is not None and metadata.get("uid") != intent.uid:
            raise WriteConflict("queued_write_uid_changed")
        if intent.revision is not None and metadata.get("resourceVersion") != intent.revision:
            raise WriteConflict("queued_write_revision_changed")

    @traced("polyad.kubernetes.request", kind=trace.SpanKind.CLIENT)
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
        Coalesce identical pending decisions before admitting another concrete write.

        Args:
            method (str): Kubernetes HTTP method.
            kind (str): Resource kind.
            namespace (str): Target namespace.
            name (str): Named resource, empty for creation or collection reads.
            body (Any): Native or typed Kubernetes document.
            status (bool): Whether to write the status subresource.
            query (list[tuple[str, str]] | None): Query parameters including dry-run selection.

        Returns:
            Any: Independent response copy; duplicate callers share one acknowledged mutation.
        """
        body = copy.deepcopy(encode_body(body))
        query = copy.deepcopy(query)
        if method in {"GET", "HEAD"}:
            return await self._request(method, kind, namespace, name, body, status=status, query=query)
        intent = write_intent(method, kind, namespace, name, body, status=status, query=query)
        contract = active_contract.get()
        proof = admission.get()
        identity: object = (
            (
                intent.target,
                intent.fingerprint,
                contract.signature() if contract else None,
                proof.batch if proof else None,
                proof.mutation.name if proof else None,
            )
            if intent
            else object()
        )
        if identity in self.pending_calls:
            decision(
                "polyad.kubernetes.write_coalesced",
                "An identical change with the same dependency contract is already pending; sharing its result.",
                key=intent.target if intent else None,
                outcome="coalesced",
                reason="identical_pending_write",
                level=logging.DEBUG,
            )
            try:
                result = copy.deepcopy(await asyncio.shield(self.pending_calls[identity]))
            except WriteConflict:
                if contract is not None:
                    await contract.refresh()
                raise
            if contract is not None and intent is not None:
                contract.advance(self, intent, result)
            return result
        if len(self.pending_calls) >= self.max_pending_writes + self.write_lock.limit:
            error = WriteConflict("write_queue_full", status=429)
            decision(
                "polyad.kubernetes.write_deferred",
                "The bounded write queue is full; retry this decision from fresh observations.",
                key=intent.target if intent else None,
                outcome="deferred",
                reason=error.conflict_reason,
                level=logging.WARNING,
            )
            if contract is not None:
                await contract.refresh()
            raise error
        result_future = asyncio.get_running_loop().create_future()
        self.pending_calls[identity] = result_future
        try:
            result = await self._request(method, kind, namespace, name, body, status=status, query=query)
        except BaseException as error:
            result_future.set_exception(WriteConflict("shared_write_cancelled") if isinstance(error, asyncio.CancelledError) else error)
            result_future.exception()  # Observe failures even when no duplicate caller joined.
            raise
        else:
            result_future.set_result(copy.deepcopy(result))
            return result
        finally:
            self.pending_calls.pop(identity, None)

    async def _request(
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
        reviews = {
            "TokenReview": ("/apis/authentication.k8s.io/v1", "tokenreviews"),
            "SubjectAccessReview": ("/apis/authorization.k8s.io/v1", "subjectaccessreviews"),
        }
        if kind == "Cluster" and (method != "GET" or status):
            raise ValueError("PostgreSQL infrastructure observations are read-only")
        prefix, plural = reviews.get(kind, BUILTINS.get(kind, (f"/apis/{GROUP}/{VERSION}", KINDS.get(kind, ""))))
        if not plural:
            raise ValueError(f"unsupported kind: {kind}")
        active = trace.get_current_span()
        active.set_attribute("k8s.resource.kind", kind)
        active.set_attribute("http.request.method", method)
        if kind in reviews and (method != "POST" or name or status or namespace):
            raise ValueError("authentication reviews require a cluster-scoped POST")
        path = f"{prefix}/{plural}" if kind in {*reviews, "CustomResourceDefinition"} else f"{prefix}/namespaces/{namespace}/{plural}"
        if name:
            path += f"/{name}"
        if status:
            path += "/status"
        started = time.monotonic()
        body = copy.deepcopy(encode_body(body))
        query = copy.deepcopy(query)
        intent = write_intent(method, kind, namespace, name, body, status=status, query=query)
        logger.debug("API request queued method=%s kind=%s namespace=%s name=%s status=%s", method, kind, namespace, name, status)
        async with self.mutation(method, intent):
            logger.debug(
                "API request dispatched method=%s kind=%s namespace=%s name=%s wait_seconds=%.3f",
                method,
                kind,
                namespace,
                name,
                time.monotonic() - started,
            )
            try:
                request = asyncio.create_task(
                    asyncio.to_thread(
                        self.client.call_api,
                        path,
                        method,
                        body=body,
                        query_params=query or [],
                        response_type="object",
                        auth_settings=["BearerToken"],
                        header_params={"Content-Type": "application/merge-patch+json" if method == "PATCH" else "application/json"},
                        _return_http_data_only=True,
                        _request_timeout=(
                            self.connection_settings["connectTimeoutSeconds"],
                            self.connection_settings["readTimeoutSeconds"],
                        ),
                    )
                )
                try:
                    result = await asyncio.shield(request)
                    contract = active_contract.get()
                    if contract is not None:
                        if method == "GET":
                            contract.record(self, (kind, namespace, name), query, result)
                        elif intent is not None:
                            contract.advance(self, intent, result)
                    logger.debug(
                        "API request completed method=%s kind=%s namespace=%s name=%s elapsed_seconds=%.3f",
                        method,
                        kind,
                        namespace,
                        name,
                        time.monotonic() - started,
                    )
                    return result
                except asyncio.CancelledError:
                    # Cancelling to_thread does not stop HTTP. Join it before ownership ends.
                    logger.debug(
                        "API request cancelled; joining outstanding transport method=%s kind=%s namespace=%s name=%s",
                        method,
                        kind,
                        namespace,
                        name,
                    )
                    while not request.done():
                        try:
                            await asyncio.shield(request)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    await asyncio.gather(request, return_exceptions=True)
                    raise
            except ApiException as error:
                if isinstance(error, WriteConflict):
                    raise
                if error.status == 409:
                    decision(
                        "polyad.kubernetes.conflict",
                        "Kubernetes rejected a competing write; refresh the resource before retrying.",
                        key=(kind, namespace, name),
                        outcome="deferred",
                        reason="resource_version_conflict",
                        level=logging.WARNING,
                        attributes={
                            "http.request.method": method,
                            "http.response.status_code": 409,
                            "polyad.target.cluster": getattr(self, "cluster", os.environ.get("POLYAD_CLUSTER_NAME", "")),
                        },
                    )
                logger.debug(
                    "API request failed method=%s kind=%s namespace=%s name=%s http_status=%s elapsed_seconds=%.3f",
                    method,
                    kind,
                    namespace,
                    name,
                    error.status,
                    time.monotonic() - started,
                )
                if error.status == 404 and method in {"GET", "DELETE"}:
                    contract = active_contract.get()
                    if contract is not None:
                        if method == "GET":
                            contract.record(self, (kind, namespace, name), query, None)
                        elif intent is not None:
                            contract.advance(self, intent, None)
                    return None
                if error.status in {404, 409} and method not in {"GET", "HEAD"}:
                    raise WriteConflict("write_target_disappeared" if error.status == 404 else "write_target_conflict") from error
                raise
            except Exception as error:
                logger.debug(
                    "API transport failed method=%s kind=%s namespace=%s name=%s error_type=%s elapsed_seconds=%.3f",
                    method,
                    kind,
                    namespace,
                    name,
                    type(error).__name__,
                    time.monotonic() - started,
                )
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
        enabled = {
            None: True,
            "mesh": os.environ.get("POLYAD_MESH_ENABLED", "false").lower() == "true",
            "capacity": os.environ.get("POLYAD_CAPACITY_ENABLED", "false").lower() == "true",
            "vpa": os.environ.get("POLYAD_VPA_ENABLED", "false").lower() == "true",
        }
        kinds = tuple(kind for kind in sorted(GRAPH_OWNED_KINDS) if enabled[RESOURCE_TYPES[kind].required_feature])
        for kind in kinds:
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
