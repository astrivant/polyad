"""
Compose application-owned predicates over public events and discovery records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from typing import Any

    from polyad_types.events.envelope import Event


@dataclass(frozen=True)
class Filter:
    """
    Combine predicates with ampersand, pipe and inversion without evaluating source strings.

    Attributes:
        predicate (Callable[[Event], bool]): Application-owned event predicate.
    """

    predicate: Callable[[Event], bool]

    def __call__(self, event: Event) -> bool:
        """
        Match one decoded event.

        Args:
            event (Event): Public observation or discovery event.

        Returns:
            bool: Whether the predicate matches.
        """
        return bool(self.predicate(event))

    def __and__(self, other: Filter) -> Filter:
        """
        Require both predicate to match.

        Args:
            other (Filter): Predicate to combine with this one.

        Returns:
            Filter: Composed application predicate.
        """
        return Filter(lambda event: self(event) and other(event))

    def __or__(self, other: Filter) -> Filter:
        """
        Require either predicate to match.

        Args:
            other (Filter): Predicate to combine with this one.

        Returns:
            Filter: Composed application predicate.
        """
        return Filter(lambda event: self(event) or other(event))

    def __invert__(self) -> Filter:
        """
        Negate this predicate.

        Returns:
            Filter: Predicate matching events rejected by this one.
        """
        return Filter(lambda event: not self(event))


def event_type(*names: str) -> Filter:
    """
    Select graph, topology, connection, discovery or stream-control event types.

    Args:
        *names (str): Exact SSE event names; multiple names are alternatives.

    Returns:
        Filter: Event-type predicate.
    """
    selected = frozenset(names)
    return Filter(lambda event: event.event in selected)


def field(path: str, *, equals: Any = None, regex: str | None = None, exists: bool | None = None) -> Filter:
    """
    Match a public payload field by exact value, regular expression or presence.

    Args:
        path (str): Dot-separated mapping keys; star traverses list elements or mapping values.
        equals (Any): Exact JSON-compatible value; omitted matches explicit null.
        regex (str | None): Trusted application regular expression, searched in string values only.
        exists (bool | None): Test presence or absence instead of value when supplied.

    Returns:
        Filter: Missing fields never match values, including null.
    """
    parts = path.split(".")
    if not path or any(not part for part in parts) or len(parts) > 16:
        raise ValueError("filter paths require 1 through 16 nonempty components")
    if regex is not None and (not isinstance(regex, str) or len(regex) > 512):
        raise ValueError("filter regex must be a string of at most 512 characters")
    if regex is not None and exists is not None:
        raise ValueError("choose a regex or a presence check")
    pattern = re.compile(regex) if regex is not None else None

    def matches(event: Event) -> bool:
        # Wildcards fan out at each path component. Retain all candidate leaves
        # so the final predicate can match any value reached through the path.
        values: list[Any] = [event.data]
        for part in parts:
            following: list[Any] = []
            for value in values:
                if part == "*":
                    if isinstance(value, dict):
                        following.extend(value.values())
                    elif isinstance(value, list):
                        following.extend(value)
                elif isinstance(value, dict) and part in value:
                    following.append(value[part])
            values = following
        if exists is not None:
            return bool(values) is exists
        if pattern is not None:
            return any(isinstance(value, str) and pattern.search(value) is not None for value in values)

        # JSON booleans must not match numeric 0/1 just because Python considers
        # them equal; require both value and concrete type to match.
        return any(type(value) is type(equals) and value == equals for value in values)

    return Filter(matches)


def graph(*, name: str | None = None, kind: str | None = None, namespace: str | None = None, cluster: str | None = None) -> Filter:
    """
    Select a graph identity consistently across observations, proposals and discovery.

    Args:
        name (str | None): Exact graph instance name.
        kind (str | None): Graph, PolyGraph or ReplicaGroup.
        namespace (str | None): Exact graph namespace.
        cluster (str | None): Exact registered cluster name.

    Returns:
        Filter: All supplied identity fields must match.
    """
    expected = {
        key: value for key, value in {"name": name, "kind": kind, "namespace": namespace, "cluster": cluster}.items() if value is not None
    }

    def matches(event: Event) -> bool:
        identity: Mapping[str, Any] = event.data.get("graph", event.data)
        return all(identity.get(key) == value for key, value in expected.items())

    return Filter(matches)


def phase(*names: str) -> Filter:
    """
    Select resource or temporary-connection lifecycle phases.

    Args:
        *names (str): Exact phases, such as Pending, Active, Running or Expired.

    Returns:
        Filter: Match the receipt status for connections and resource status otherwise.
    """
    selected = frozenset(names)
    return Filter(lambda event: event.data.get("connection", event.data).get("status", {}).get("phase") in selected)


def connection_pending(node: str | None = None) -> Filter:
    """
    Select proposals still awaiting consent, optionally for one logical endpoint.

    Args:
        node (str | None): Endpoint name; omitted selects all pending proposals in the authorized stream.

    Returns:
        Filter: Predicate only; matching never approves a proposal automatically.
    """

    def matches(event: Event) -> bool:
        receipt = event.data.get("connection", {})
        target = receipt.get("target", {})
        if receipt.get("peers") and node is not None:
            local = event.data.get("participant")
            return (
                local in receipt["peers"]
                and receipt["peers"][local]["node"] == node
                and target.get(local) not in receipt.get("consent", {})
            )
        return node is None or (node in {target.get("source"), target.get("target")} and node not in receipt.get("consent", {}))

    return event_type("connection") & phase("Pending") & Filter(matches)
