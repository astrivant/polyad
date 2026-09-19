"""
Compile compact queue envelopes for inexpensive service-side checks.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass

from polyad_sdk.symbiosis.reachability.models import Interaction, QueueModel, Relationship, finite


@dataclass(frozen=True)
class Envelope:
    """
    Store a finite-horizon fluid-queue contract and its applicability window.

    The queue model has an analytic worst-case bound, so guards need no grid or
    numerical solver. Optional HJ computations independently study the same
    queue-overflow boundary. They do not turn numerical values into certificates.

    Attributes:
        model (QueueModel): Validated dynamics, approved routing and terminal target.
        revision (str): Application-owned revision covering topology, capacity and permissions.
        horizon (float): Contract duration starting at the observation time, in seconds.
        created_at (float): Artifact creation time in Unix seconds.
        expires_at (float): Last time this artifact may be used.
    """

    model: QueueModel
    revision: str
    horizon: float
    created_at: float
    expires_at: float

    def __post_init__(self) -> None:
        """
        Require a bounded horizon and an explicit application revision and lifetime.

        Returns:
            None: Invalid artifact metadata raises ValueError.
        """
        if not isinstance(self.model, QueueModel) or not isinstance(self.revision, str) or not self.revision.strip():
            raise ValueError("an envelope needs a model and application revision")
        for value in (self.horizon, self.created_at, self.expires_at):
            finite(value)
        if not 0 < self.horizon <= 3600 or not self.created_at < self.expires_at <= self.created_at + 86400:
            raise ValueError("use a horizon up to one hour and an artifact lifetime up to one day")

    def assess(self, state: tuple[float, ...]) -> tuple[bool, float]:
        """
        Check queue safety throughout the horizon and the target at its endpoint.

        Args:
            state (tuple[float, ...]): Upper bounds on the current state, in model axis order.

        Returns:
            tuple[bool, float]: Whether all bounds pass, and the smallest queue slack in work units.
        """
        peak, terminal = self.model.bounds(state, self.horizon)
        margin = min(
            *(limit - value for limit, value in zip(self.model.limits, peak, strict=True)),
            *(target - value for target, value in zip(self.model.targets, terminal, strict=True)),
        )
        return margin >= 0, margin

    def dumps(self) -> str:
        """
        Serialize a small portable artifact without Python executable objects.

        Returns:
            str: Versioned JSON with a model fingerprint for consistency checking.
        """
        return json.dumps({"version": 1, "fingerprint": self.model.fingerprint, **asdict(self)}, sort_keys=True, allow_nan=False)

    @classmethod
    def loads(cls, source: str) -> Envelope:
        """
        Validate a trusted, bounded JSON artifact before use by a service.

        The fingerprint detects inconsistency, not forgery. Distribute artifacts
        through the application's authenticated configuration path.

        Args:
            source (str): JSON from Envelope.dumps, limited to 64 KiB.

        Returns:
            Envelope: Revalidated model and metadata; malformed artifacts raise ValueError.
        """
        if len(source.encode()) > 65536:
            raise ValueError("envelope exceeds 64 KiB")
        try:
            document = json.loads(source)
            version = document.pop("version")
            if type(version) is not int or version != 1:
                raise ValueError("unsupported envelope version")
            fingerprint = document.pop("fingerprint")
            model = document.pop("model")
            interaction = model.pop("interaction")
            model["interaction"] = Interaction(Relationship(interaction["relationship"]), tuple(interaction["effects"]))
            for key in ("names", "capacities", "limits", "targets", "arrival_bounds", "shares"):
                model[key] = tuple(model[key])
            envelope = cls(model=QueueModel(**model), **document)
            if envelope.model.fingerprint != fingerprint:
                raise ValueError("envelope model fingerprint differs")
            return envelope
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("malformed envelope") from error


def compile_envelope(model: QueueModel, revision: str, *, horizon: float = 10, lifetime: float = 300) -> Envelope:
    """
    Prepare the exact fixed-routing bound for this supported fluid-queue model.

    Args:
        model (QueueModel): Calibrated model with explicit work units and routing shares.
        revision (str): Application revision to match at each guard check.
        horizon (float): Duration of the queue-safety and terminal-target contract.
        lifetime (float): Artifact validity in seconds; revision changes invalidate it sooner.

    Returns:
        Envelope: Compact contract evaluable with the Python standard library.
    """
    finite(lifetime)
    now = time.time()
    return Envelope(model, revision, horizon, now, now + lifetime)
