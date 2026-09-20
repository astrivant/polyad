"""
Describe expiring, application-assessed offers of shared work and resources.
"""

from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING

from attrs import field, fields, frozen
from cattrs.gen import make_dict_structure_fn, override

from polyad_types.api.discovery import ServiceEndpoint
from polyad_types.serialization import converter

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import Any

__all__ = (
    "CapabilityAdvertisement",
    "CapabilityContract",
    "CapabilityOffer",
    "ResourceAvailability",
)


@frozen
class CapabilityOffer:
    """
    State both spare capacity and the provider's willingness to share one work type.

    Attributes:
        name (str): Application-defined work type, such as image.resize.
        unit (str): Work unit counted per second, such as images or bytes.
        availablePerSecond (float): Estimated additional sustainable throughput.
        sharePerSecond (float): Maximum throughput the application wishes to share.
        availableConcurrency (int | None): Additional simultaneous work slots, if known.
        shareConcurrency (int | None): Maximum simultaneous slots offered to peers, if known.
    """

    name: str = field(metadata={"schema": {"minLength": 1, "maxLength": 64}})
    unit: str = field(metadata={"schema": {"minLength": 1, "maxLength": 64}})
    availablePerSecond: float = field(metadata={"schema": {"minimum": 0}})
    sharePerSecond: float = field(metadata={"schema": {"minimum": 0}})
    availableConcurrency: int | None = field(default=None, metadata={"schema": {"minimum": 0}})
    shareConcurrency: int | None = field(default=None, metadata={"schema": {"minimum": 0}})

    def __attrs_post_init__(self) -> None:
        """
        Reject ambiguous units, nonfinite rates and partially specified slot limits.

        Returns:
            None: Invalid capacity raises ValueError.
        """
        for value in (self.name, self.unit):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,63}", value):
                raise ValueError("capability names and units must be bounded, nonempty identifiers")
        for rate in (self.availablePerSecond, self.sharePerSecond):
            if type(rate) not in (float, int) or not math.isfinite(rate) or rate < 0:
                raise ValueError("capability rates must be finite nonnegative numbers")
        for slots in (self.availableConcurrency, self.shareConcurrency):
            if slots is not None and (type(slots) is not int or not 0 <= slots <= 2**53 - 1):
                raise ValueError("capability concurrency must be a nonnegative JSON-safe integer")
        if (self.availableConcurrency is None) != (self.shareConcurrency is None):
            raise ValueError("available and shared concurrency must be supplied together")

    @property
    def offered_per_second(self) -> float:
        """
        Clamp the offered rate to both ability and willingness.

        Returns:
            float: Additional work per second; not a reservation or completed-work measurement.
        """
        return min(self.availablePerSecond, self.sharePerSecond)

    @property
    def offered_concurrency(self) -> int | None:
        """
        Clamp optional simultaneous slots to the smaller declared budget.

        Returns:
            int | None: Shared slots, or None when the provider does not measure slots.
        """
        if self.availableConcurrency is None or self.shareConcurrency is None:
            return None
        return min(self.availableConcurrency, self.shareConcurrency)


@frozen
class ResourceAvailability:
    """
    Disclose one container's resource observations, not an independent pool per work type.

    Attributes:
        container (str): Container sampled by the provider; empty when not projected.
        cpuLimitMillicores (int | None): Live CPU quota; not spare CPU or a utilization rate.
        cpuUsageUsec (int | None): Cumulative cgroup CPU time, not CPU availability.
        memoryLimitBytes (int | None): Live memory ceiling.
        memoryUsageBytes (int | None): Live cgroup memory usage.
        cpuRequestMillicores (int | None): Startup request, which can lag in-place resizing.
        memoryRequestBytes (int | None): Startup memory request, not a live allocation.
        vpaMinCpuMillicores (int | None): Projected VPA CPU lower bound.
        vpaMaxCpuMillicores (int | None): Projected VPA CPU upper bound.
        vpaMinMemoryBytes (int | None): Projected VPA memory lower bound.
        vpaMaxMemoryBytes (int | None): Projected VPA memory upper bound.
    """

    container: str = ""
    cpuLimitMillicores: int | None = None
    cpuUsageUsec: int | None = None
    memoryLimitBytes: int | None = None
    memoryUsageBytes: int | None = None
    cpuRequestMillicores: int | None = None
    memoryRequestBytes: int | None = None
    vpaMinCpuMillicores: int | None = None
    vpaMaxCpuMillicores: int | None = None
    vpaMinMemoryBytes: int | None = None
    vpaMaxMemoryBytes: int | None = None

    def __attrs_post_init__(self) -> None:
        """
        Preserve unknown readings while rejecting negative or coerced observations.

        Returns:
            None: Invalid observations raise ValueError.
        """
        if not isinstance(self.container, str) or len(self.container) > 63:
            raise ValueError("container name must be a string of at most 63 characters")
        for item in fields(type(self)):
            value = getattr(self, item.name)
            if item.name != "container" and value is not None and (type(value) is not int or not 0 <= value <= 2**53 - 1):
                raise ValueError("resource readings must be nonnegative JSON-safe integers or None")
        for lower, upper in ((self.vpaMinCpuMillicores, self.vpaMaxCpuMillicores), (self.vpaMinMemoryBytes, self.vpaMaxMemoryBytes)):
            if lower is not None and upper is not None and lower > upper:
                raise ValueError("VPA minimum must not exceed maximum")

    @property
    def memory_available_bytes(self) -> int | None:
        """
        Calculate observed memory headroom without treating unknown limits as capacity.

        Returns:
            int | None: Nonnegative headroom, or None when either reading is unavailable.
        """
        if self.memoryLimitBytes is None or self.memoryUsageBytes is None:
            return None
        return max(0, self.memoryLimitBytes - self.memoryUsageBytes)


@frozen
class CapabilityAdvertisement:
    """
    Replace one Pod's complete TTL-bound offer contract for a logical service.

    Attributes:
        endpoint (ServiceEndpoint): Exact graph incarnation and logical service.
        capabilities (tuple[CapabilityOffer, ...]): Unique work types sharing one provider pool; empty withdraws the contract.
        labels (dict[str, str]): Application group selectors, never authorization claims.
        resources (ResourceAvailability | None): Optional disclosure of one shared container resource pool.
        ttlSeconds (int): Server-timed lifetime from 5 through 300 seconds.
    """

    endpoint: ServiceEndpoint
    capabilities: tuple[CapabilityOffer, ...] = field(metadata={"schema": {"maxItems": 32}})
    labels: dict[str, str] = field(factory=dict, metadata={"schema": {"maxProperties": 16}})
    resources: ResourceAvailability | None = None
    ttlSeconds: int = field(default=30, metadata={"schema": {"minimum": 5, "maximum": 300}})

    def __attrs_post_init__(self) -> None:
        """
        Bound replacement work and reject ambiguous or unsafe identity declarations.

        Returns:
            None: Invalid contracts raise ValueError.
        """
        if not isinstance(self.endpoint, ServiceEndpoint):
            raise ValueError("advertisements require a typed service endpoint")
        if (
            not isinstance(self.capabilities, tuple)
            or len(self.capabilities) > 32
            or any(not isinstance(item, CapabilityOffer) for item in self.capabilities)
            or len({item.name for item in self.capabilities}) != len(self.capabilities)
        ):
            raise ValueError("advertisements require at most 32 unique capability offers")
        if not isinstance(self.labels, dict) or len(self.labels) > 16:
            raise ValueError("advertisements allow at most 16 group labels")
        for key, value in self.labels.items():
            if not isinstance(key, str) or not isinstance(value, str) or not 1 <= len(key) <= 128 or len(value) > 128:
                raise ValueError("label keys and values must be bounded strings")
            if any(ord(char) < 32 or ord(char) == 127 for char in key + value):
                raise ValueError("labels cannot contain control characters")
        if self.resources is not None and not isinstance(self.resources, ResourceAvailability):
            raise ValueError("resources require a typed resource observation")
        if type(self.ttlSeconds) is not int or not 5 <= self.ttlSeconds <= 300:
            raise ValueError("advertisement TTL must be from 5 through 300 seconds")


@frozen
class CapabilityContract:
    """
    Return a server-timed, Pod-fenced advertisement discovered through graph grants.

    Attributes:
        advertisement (CapabilityAdvertisement): Provider's complete declared sharing policy.
        podName (str): Verified publishing Pod name.
        podUid (str): Exact replica incarnation; siblings advertise independently.
        observedAt (float): Server receipt time in UTC Unix seconds.
        expiresAt (float): Server expiry time in UTC Unix seconds.
    """

    advertisement: CapabilityAdvertisement
    podName: str
    podUid: str
    observedAt: float
    expiresAt: float

    def __attrs_post_init__(self) -> None:
        """
        Validate replica fences and a finite, bounded lifetime.

        Returns:
            None: Invalid server records raise ValueError.
        """
        if not isinstance(self.advertisement, CapabilityAdvertisement):
            raise ValueError("contract requires a typed advertisement")
        if any(not isinstance(value, str) or not 1 <= len(value) <= 253 for value in (self.podName, self.podUid)):
            raise ValueError("contract requires bounded Pod name and UID")
        if any(type(value) not in (float, int) or not math.isfinite(value) or value < 0 for value in (self.observedAt, self.expiresAt)):
            raise ValueError("contract timestamps must be finite nonnegative numbers")
        if not 0 < self.expiresAt - self.observedAt <= 300:
            raise ValueError("contract expiry must be within 300 seconds of receipt")


def _endpoint(value: Any, _: Any) -> ServiceEndpoint:
    if not isinstance(value, dict) or any(not isinstance(item, str) for item in value.values()):
        raise ValueError("endpoint identity fields must be strings")
    return converter.structure(value, ServiceEndpoint)


def _capabilities(value: Any, _: Any) -> tuple[CapabilityOffer, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("capabilities must be a JSON array of at most 32 offers")
    return tuple(converter.structure(item, CapabilityOffer) for item in value)


def _checked_structure(model: type[Any], overrides: dict[str, Any]) -> Callable[[Any, Any], Any]:
    generated = make_dict_structure_fn(model, converter, **overrides)

    def checked(value: Any, _: Any) -> Any:
        # Missing fields and non-object nested values are malformed client input,
        # not unexpected server failures or permission to withdraw a contract.
        if not isinstance(value, dict):
            raise ValueError("capability models require JSON objects")
        try:
            return generated(value, model)
        except KeyError as error:
            raise ValueError("capability model is missing a required field") from error

    return checked


# Preserve primitive input types so JSON booleans and strings cannot become
# plausible numeric budgets through cattrs' normal scalar coercion.
for _model in (CapabilityOffer, ResourceAvailability, CapabilityAdvertisement, CapabilityContract):
    _overrides: dict[str, Any] = {
        item.name: override(struct_hook=lambda value, _: value)
        for item in fields(_model)
        if item.name not in {"endpoint", "capabilities", "resources", "advertisement"}
    }
    if _model is CapabilityAdvertisement:
        _overrides["endpoint"] = override(struct_hook=_endpoint)
        _overrides["capabilities"] = override(struct_hook=_capabilities)
    converter.register_structure_hook(_model, _checked_structure(_model, _overrides))
