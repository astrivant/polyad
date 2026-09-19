"""
Describe bounded advance capacity requests independently of a node autoscaler.
"""

from __future__ import annotations

from typing import Literal

from attrs import field, frozen
from cattrs.gen import make_dict_structure_fn, override

from polyad_types.serialization import converter


@frozen
class CapacityTuning:
    """
    Select forecast depth and reservation budget without changing workload resource requests.

    Attributes:
        lookaheadStages (int): Missing dependency layers to prepare, bounded by the approved ceiling.
        maxPods (int): Maximum outstanding forecast Pods, also bounded by operator configuration.
    """

    lookaheadStages: int = field(metadata={"schema": {"minimum": 1, "maximum": 32}})
    maxPods: int = field(metadata={"schema": {"minimum": 1, "maximum": 1024}})

    def __attrs_post_init__(self) -> None:
        """
        Validate the mathematical profile before it can be selected.

        Returns:
            None: Invalid values raise ValueError.
        """
        for name, maximum in (("lookaheadStages", 32), ("maxPods", 1024)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"capacity tuning {name} must be an integer from 1 through {maximum}")


converter.register_structure_hook(
    CapacityTuning,
    make_dict_structure_fn(
        CapacityTuning,
        converter,
        lookaheadStages=override(struct_hook=lambda value, _: value),
        maxPods=override(struct_hook=lambda value, _: value),
    ),
)


@frozen(kw_only=True)
class CapacityPlan:
    """
    Forecast missing execution nodes before their admission dependencies finish.

    Attributes:
        backend (Literal['Auto', 'ProvisioningRequest', 'Placeholders']): Preferred capacity mechanism.
        lookaheadStages (int): Number of uncreated dependency layers to forecast at each boundary.
        maxPods (int): Maximum forecast Pods held by this boundary at once.
        timeoutSeconds (int): Maximum time to hold an unconsumed request, including gate waits.
        provisioningClassName (str): Autoscaler provisioning class; empty uses the operator default.
        parameters (dict[str, str]): Provider-specific ProvisioningRequest parameters.
        retryToken (str): Change this value to retry a failed or expired plan.
    """

    backend: Literal["Auto", "ProvisioningRequest", "Placeholders"] = "Auto"
    lookaheadStages: int = field(default=1, metadata={"schema": {"minimum": 1, "maximum": 32}})
    maxPods: int = field(default=128, metadata={"schema": {"minimum": 1, "maximum": 1024}})
    timeoutSeconds: int = field(default=600, metadata={"schema": {"minimum": 10, "maximum": 86400}})
    provisioningClassName: str = ""
    parameters: dict[str, str] = field(
        factory=dict, metadata={"schema": {"maxProperties": 100, "additionalProperties": {"type": "string", "maxLength": 255}}}
    )
    retryToken: str = ""

    def __attrs_post_init__(self) -> None:
        """
        Reject unbounded forecasts and unsupported backend choices.

        Returns:
            None: No return value.
        """
        if self.backend not in {"Auto", "ProvisioningRequest", "Placeholders"}:
            raise ValueError("unsupported capacity backend")
        for name, lower, upper in (("lookaheadStages", 1, 32), ("maxPods", 1, 1024), ("timeoutSeconds", 10, 86400)):
            value = getattr(self, name)
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError(f"capacity {name} must be between {lower} and {upper}")
        if len(self.parameters) > 100 or any(len(value) > 255 for value in self.parameters.values()):
            raise ValueError("capacity provisioning parameters exceed API limits")
        if self.provisioningClassName == "check-capacity.autoscaling.x-k8s.io":
            raise ValueError("capacity plans require a provisioning class that scales out")
