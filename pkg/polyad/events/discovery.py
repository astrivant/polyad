"""
Discover current application graph services through authorized atlas branches.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from polyad.api.http.errors import Forbidden, Unavailable
from polyad.events.access import configuration, require_scope
from polyad.events.topology import topology_snapshot
from polyad.events.visibility import observation_ancestry, permitted_observation, public_observation
from polyad_types.api.discovery import AccessMode
from polyad_types.resources import BOUNDARY_KINDS

if TYPE_CHECKING:
    from typing import Any

    from polyad.events.store import EventStore
    from polyad.operator.adapters.kubernetes import API
    from polyad.operator.clusters.federation import Federation
    from polyad_types.api.auth import APIKey, GraphAccess

__all__ = ("Directory",)


class Directory:
    """
    Read only current permitted graph identities and public runtime metadata.
    """

    def __init__(self, api: API, namespace: str, federation: Federation, streams: dict[str, EventStore]) -> None:
        """
        Share administrator-registered transports and existing event streams.

        Args:
            api (API): Local read adapter.
            namespace (str): Local operator namespace.
            federation (Federation): Registered cluster resolver; no caller-supplied URLs.
            streams (dict[str, EventStore]): Available local and root-held cluster event streams.
        """
        self.api, self.namespace, self.federation, self.streams = api, namespace, federation, streams

    def resolve(self, cluster: str) -> tuple[API, str]:
        """
        Resolve only clusters this operator can currently serve.

        Args:
            cluster (str): Registered execution cluster.

        Returns:
            tuple[API, str]: Cluster adapter and permitted namespace.
        """
        if not cluster or cluster == self.federation.name:
            return self.api, self.namespace
        if cluster not in self.streams:
            raise Unavailable("this operator cannot serve the requested cluster's discovery and event stream")
        return self.federation.target(cluster)

    async def graph(self, scope: GraphAccess) -> tuple[API, dict[str, Any], list[dict[str, Any]]]:
        """
        Refresh one graph and its verified cross-cluster ownership path.

        Args:
            scope (GraphAccess): Exact graph address, optionally fenced to an incarnation.

        Returns:
            tuple[API, dict[str, Any], list[dict[str, Any]]]: Adapter, graph document and canonical ancestry including itself.
        """
        cluster = scope.cluster or self.federation.name
        api, namespace = self.resolve(cluster)
        if scope.namespace != namespace:
            raise Forbidden("requested graph is outside this operator's registered namespace")
        obj = await api.get(scope.kind, namespace, scope.name)
        if obj is None or obj["metadata"].get("deletionTimestamp") or (scope.uid and obj["metadata"]["uid"] != scope.uid):
            raise KeyError("graph is absent or replaced")
        reserved = (self.namespace, os.environ["POLYAD_SELF_GRAPH"]) if os.environ.get("POLYAD_SELF_GRAPH") and api is self.api else None
        if not await public_observation(api, obj, reserved_graph=reserved):
            raise Forbidden("graph is not available for application discovery")
        ancestry = await observation_ancestry(api, obj, cluster=cluster, resolve=self.resolve)
        identity = {"cluster": cluster, "kind": scope.kind, **{name: obj["metadata"][name] for name in ("namespace", "name", "uid")}}
        return api, obj, [identity, *ancestry]

    async def authorize_stream(self, key: APIKey | None, cluster: str | None) -> None:
        """
        Reject unsupported subscriptions before allocating a live event stream.

        Args:
            key (APIKey | None): Named subscriber; None retains the existing namespace-token or demo policy.
            cluster (str | None): Explicit registered stream selection.

        Returns:
            None: Invalid modes, missing home and unavailable cluster streams reject the HTTP request.
        """
        selected = cluster or self.federation.name
        mode = configuration().effective(self.federation.name, "discovery")
        if mode == AccessMode.DISABLED:
            raise Forbidden("discovery and event subscriptions are disabled by this operator mode")
        self.resolve(selected)
        if key is None:
            if mode in {AccessMode.SAME_GRAPH, AccessMode.GRAPH_TREE}:
                raise Forbidden("this discovery mode requires a named credential with a fixed home graph")
            if selected != self.federation.name and (
                mode != AccessMode.ATLAS or configuration().effective(selected, "discovery") != AccessMode.ATLAS
            ):
                raise Forbidden("cross-cluster events require this operator Atlas discovery mode")
            return
        if key.home is None:
            raise Forbidden("named event credentials require an administrator-assigned home graph")
        _, _, home = await self.graph(key.home)
        target = home if selected == home[0]["cluster"] else [{**home[0], "cluster": selected}]
        require_scope("discovery", home, target)
        require_scope("discovery", target, home)

    async def permits(self, key: APIKey, identity: dict[str, Any]) -> bool:
        """
        Recheck observation scope against live ancestry and the subscriber's fixed home.

        Args:
            key (APIKey): Current named subscriber credential.
            identity (dict[str, Any]): Public graph identity carried by a snapshot or event.

        Returns:
            bool: False for missing home, replaced graphs, scope denial or unavailable ancestry.
        """
        from polyad_types.api.auth import GraphAccess

        if key.home is None:
            return False
        try:
            _, _, home = await self.graph(key.home)
            scope = GraphAccess(
                identity["name"], identity["namespace"], kind=identity["kind"], cluster=identity.get("cluster", ""), uid=identity["uid"]
            )
            _, _, path = await self.graph(scope)
            require_scope("discovery", home, path)
            require_scope("discovery", path, home)
            return permitted_observation(path[0], path[1:], key.graphs)
        except (Forbidden, KeyError):
            return False

    async def discover(self, key: APIKey, target: GraphAccess | None = None, *, offset: int = 0, limit: int = 100) -> dict[str, Any]:
        """
        List authorized starting graphs or one graph's current service vertices.

        Args:
            key (APIKey): Authenticated credential with fixed home and graph grants.
            target (GraphAccess | None): Selected boundary; omitted enumerates credential-granted roots.
            offset (int): Starting index within the credential's graph grants.
            limit (int): Maximum root grants examined in this response, from 1 through 100.

        Returns:
            dict[str, Any]: Roots or service records, explicit child graph links, and per-cluster replay cursors.
        """
        if key.home is None:
            raise Forbidden("discovery requires an administrator-assigned home graph")
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("discovery offset must be nonnegative and limit from 1 through 100")
        _, _, home = await self.graph(key.home)
        require_scope("discovery", home, home)
        if target is None:
            roots = [home[0]] if offset == 0 and permitted_observation(home[0], home[1:], key.graphs) else []
            for scope in key.graphs[offset : offset + limit]:
                try:
                    _, _, path = await self.graph(scope)
                    require_scope("discovery", home, path)
                    require_scope("discovery", path, home)
                except (Forbidden, KeyError):
                    continue
                if path[0] not in roots:
                    roots.append(path[0])
            return {"roots": roots, "nextOffset": offset + limit if offset + limit < len(key.graphs) else None}
        api, obj, path = await self.graph(target)
        require_scope("discovery", home, path)
        require_scope("discovery", path, home)
        if not permitted_observation(path[0], path[1:], key.graphs):
            raise Forbidden("credential does not grant discovery of this graph tree")
        cluster = path[0]["cluster"]
        stream = self.streams.get(cluster)
        if stream is None:
            raise Unavailable("this operator cannot supply events for the requested graph")

        # Capture before reading live membership. Replaying concurrent events can
        # duplicate an observation, but cannot leave a read/subscribe gap.
        cursor = await stream.cursor(None)
        children = await api.owned(target.namespace, obj["metadata"]["uid"])
        from polyad.operator.clusters.federation import Federation

        reader = Federation(api)
        reader.name, reader.resolver = cluster, self.resolve
        children.extend(await reader.children(obj))
        unique = {child["metadata"]["uid"]: child for child in children}
        snapshot = await topology_snapshot(api, obj, list(unique.values()))
        if not snapshot["valid"]:
            raise Unavailable("graph topology cannot currently fulfill discovery")
        services, branches = [], []
        for node in snapshot["nodes"]:
            if not node["desired"]:
                continue
            services.append(
                {
                    "graph": path[0],
                    "node": node,
                    "revision": snapshot["revision"],
                    "endpoint": {
                        "cluster": cluster,
                        "namespace": target.namespace,
                        "kind": obj["kind"],
                        "graph": target.name,
                        "graphUid": path[0]["uid"],
                        "node": node["name"],
                    },
                }
            )
            for execution in node["executions"]:
                if execution["kind"] in BOUNDARY_KINDS and not execution["terminating"]:
                    branches.append(
                        {
                            "cluster": execution.get("cluster", cluster),
                            "namespace": execution.get("namespace", target.namespace),
                            "kind": execution["kind"],
                            "name": execution["name"],
                            "uid": execution["uid"],
                        }
                    )
        return {"graph": path[0], "services": services, "children": branches, "cursors": {cluster: cursor}}
