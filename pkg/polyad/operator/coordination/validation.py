"""
Validate bounded pending writes ahead of dispatch and invalidate receipts on observations.
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from weakref import WeakSet

from polyad.operator.coordination.contracts import without_capture
from polyad.operator.coordination.write_queue import WriteConflict

if TYPE_CHECKING:
    from polyad.operator.adapters.kubernetes import API
    from polyad.operator.coordination.contracts import ReadContract
    from polyad.operator.coordination.queue import Key
    from polyad.operator.coordination.write_queue import WriteIntent


@dataclass(frozen=True)
class ValidationSettings:
    """
    Bound validator reads and the lifetime of reusable validation receipts.

    Attributes:
        interval (float): Maximum pause between background validation passes.
        window (float): Receipt lifetime measured from the start of validation.
        burst (int): Maximum candidates examined in one background pass.
        workers (int): Maximum concurrent candidate validations, including dispatch checks.
    """

    interval: float = 1
    window: float = 5
    burst: int = 8
    workers: int = 1

    def __post_init__(self) -> None:
        """
        Reject invalid budgets before starting any validation worker.

        Returns:
            None: Every bounded pass can revisit the earliest expiring receipt.
        """
        if (
            not all(type(value) in (int, float) and math.isfinite(value) for value in (self.interval, self.window))
            or not 0.01 <= self.interval <= self.window <= 60
        ):
            raise ValueError("write validation requires 0.01 <= interval <= window <= 60 seconds")
        if type(self.burst) is not int or not 1 <= self.burst <= 128:
            raise ValueError("write validation burst must be an integer between 1 and 128")
        if type(self.workers) is not int or not 1 <= self.workers <= 32:
            raise ValueError("write validation workers must be an integer between 1 and 32")

    @classmethod
    def from_environment(cls) -> ValidationSettings:
        """
        Load chart-projected producer settings with bounded defaults.

        Returns:
            ValidationSettings: Validated interval, freshness window and per-pass burst.
        """
        return cls(
            interval=float(os.environ.get("POLYAD_WRITE_VALIDATION_INTERVAL_SECONDS", "1")),
            window=float(os.environ.get("POLYAD_WRITE_VALIDATION_WINDOW_SECONDS", "5")),
            burst=int(os.environ.get("POLYAD_WRITE_VALIDATION_BURST", "8")),
            workers=int(os.environ.get("POLYAD_WRITE_VALIDATION_WORKERS", "1")),
        )


@dataclass(eq=False)
class Validation:
    """
    Share one pending write's validation between the producer and dispatcher.

    Attributes:
        api (API): Destination adapter.
        intent (WriteIntent | None): Named write and original target fences.
        contract (ReadContract | None): Frozen dependency expectations at enqueue time.
        check_target (bool): Whether this write waited behind another operation.
        settings (ValidationSettings): Freshness and read budgets.
        wake (asyncio.Event): Wake the producer after a relevant observation.
        slots (asyncio.Semaphore): Shared bound for background and dispatcher validation.
        generation (int): Invalidations since enqueue.
        checked_generation (int): Invalidation generation captured at validation start.
        checked_at (float | None): Monotonic validation start, excluding no read latency.
        error (WriteConflict | None): Known drift which requires a new decision.
        task (asyncio.Task[None] | None): Shared in-progress validation.
    """

    api: API
    intent: WriteIntent | None
    contract: ReadContract | None
    check_target: bool
    settings: ValidationSettings
    wake: asyncio.Event
    slots: asyncio.Semaphore
    generation: int = 0
    checked_generation: int = -1
    checked_at: float | None = None
    error: WriteConflict | None = None
    task: asyncio.Task[None] | None = None

    def depends_on(self, api: API, key: Key) -> bool:
        """
        Match a changed named resource against targets and collection dependencies.

        Args:
            api (API): Cluster adapter which observed the change.
            key (Key): Changed resource identity.

        Returns:
            bool: Whether this receipt must be invalidated conservatively.
        """
        if key[0] == "*":
            return api is self.api or (
                self.contract is not None and any(observation.api is api for observation in self.contract.reads.values())
            )
        if api is self.api and self.intent is not None and self.intent.target == key:
            return True
        return self.contract is not None and any(
            observation.api is api and observation.key[:2] == key[:2] and observation.key[2] in {"", key[2]}
            for observation in self.contract.reads.values()
        )

    def fresh(self) -> bool:
        """
        Check receipt age, invalidations and overlapping in-flight writes.

        Returns:
            bool: A reusable receipt never survives a relevant known mutation.
        """
        if self.error or self.checked_at is None or self.checked_generation != self.generation:
            return False
        if self.contract is not None and self.contract.expired():
            return False
        if time.monotonic() - self.checked_at >= self.settings.window:
            return False
        adapters = {self.api}
        if self.contract is not None:
            adapters.update(observation.api for observation in self.contract.reads.values())
        return not any(self.depends_on(api, key) for api in adapters for key in getattr(api, "active_write_targets", ()))

    async def run(self) -> None:
        """
        Produce a validation receipt without replacing the original dependency contract.

        Returns:
            None: Failures are retained for the dispatcher to request fresh reconciliation.
        """

        # A validation covers only the generation it started with; concurrent invalidation stays pending.
        started, generation = time.monotonic(), self.generation
        try:
            async with self.slots:
                with without_capture():
                    if self.contract is not None:
                        await self.contract.validate()
                    if self.check_target and self.intent is not None:
                        await self.api.revalidate_write(self.intent)
        except WriteConflict as error:
            self.error = error
        except Exception:
            self.error = WriteConflict("dependency_observation_unavailable")
        else:
            self.checked_at, self.checked_generation = started, generation

    async def ensure(self) -> None:
        """
        Consume a fresh receipt or join one refreshed validation of this candidate.

        Returns:
            None: Expiry, concurrent invalidation and observed drift refuse dispatch.
        """
        if self.error:
            raise self.error
        if self.fresh():
            return
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.run())
        await asyncio.shield(self.task)
        if self.error:
            raise self.error
        if not self.fresh():
            raise WriteConflict("validation_window_expired_or_invalidated")


receipts: WeakSet[Validation] = WeakSet()


def invalidate(api: API, key: Key) -> None:
    """
    Invalidate affected queued receipts from watches, scans or concrete write transitions.

    Args:
        api (API): Adapter identifying the observed cluster.
        key (Key): Changed resource, including a disappeared object.

    Returns:
        None: An event only requests validation; it never authorizes a write.
    """
    for receipt in list(receipts):
        if receipt.depends_on(api, key):
            receipt.generation += 1
            receipt.wake.set()


@dataclass
class ValidationQueue:
    """
    Walk the pending queue in bounded bursts on the existing operator event loop.

    Attributes:
        settings (ValidationSettings): Validated producer budgets.
        pending (dict[int, Validation]): Candidates ordered by enqueue token.
        wake (asyncio.Event): New work or watch invalidation notification.
        task (asyncio.Task[None] | None): One transient producer while pending work exists.
        slots (asyncio.Semaphore): Bound outstanding candidate validations across consumers.
    """

    settings: ValidationSettings = field(default_factory=ValidationSettings.from_environment)
    pending: dict[int, Validation] = field(default_factory=dict)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    slots: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        """
        Share one concurrency bound across producer and dispatcher validation tasks.

        Returns:
            None: Each candidate still owns at most one outstanding validation task.
        """
        self.slots = asyncio.Semaphore(self.settings.workers)

    def add(self, token: int, api: API, intent: WriteIntent | None, contract: ReadContract | None, waited: bool) -> Validation:
        """
        Pair a pending candidate with frozen dependencies and start the producer if needed.

        Args:
            token (int): Concrete write backlog token.
            api (API): Destination cluster adapter.
            intent (WriteIntent | None): Original write identity and version fences.
            contract (ReadContract | None): Decision read set to freeze at enqueue time.
            waited (bool): Whether dispatch was already occupied.

        Returns:
            Validation: Candidate shared with the dispatcher.
        """
        receipt = Validation(api, intent, contract.clone() if contract else None, waited, self.settings, self.wake, self.slots)
        self.pending[token] = receipt
        receipts.add(receipt)
        self.wake.set()
        if self.task is None or self.task.done() or self.task.cancelling():
            with without_capture():
                self.task = asyncio.create_task(self.run())
        return receipt

    async def run(self) -> None:
        """
        Prioritize invalid or expiring receipts, then use the remaining burst for later items.

        Returns:
            None: Sleeps between passes and exits as soon as the queue drains.
        """

        async def examine(receipt: Validation) -> None:
            """
            Validate an eligible candidate without failing other producer workers.

            Args:
                receipt (Validation): Pending candidate selected for this bounded pass.

            Returns:
                None: Drift is retained on the candidate for targeted reconciliation.
            """
            if receipt not in self.pending.values() or receipt.error or receipt.fresh():
                return
            try:
                await receipt.ensure()
            except WriteConflict:
                pass
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise

        while self.pending:
            self.wake.clear()
            started = time.monotonic()
            candidates = sorted(
                (receipt for receipt in self.pending.values() if not receipt.error and not receipt.fresh()),
                key=lambda item: item.checked_at if item.checked_at is not None else -1,
            )[: self.settings.burst]
            for offset in range(0, len(candidates), self.settings.workers):
                await asyncio.gather(*(examine(receipt) for receipt in candidates[offset : offset + self.settings.workers]))
                if time.monotonic() - started >= self.settings.window:
                    break
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=self.settings.interval)
            except TimeoutError:
                pass

    async def remove(self, token: int) -> None:
        """
        Release a candidate and join any validation reads before its adapter can close.

        Args:
            token (int): Dispatched, rejected or cancelled backlog entry.

        Returns:
            None: No validation tasks outlive their final pending caller.
        """
        receipt = self.pending.pop(token, None)
        if receipt is None:
            return
        receipts.discard(receipt)
        tasks = []
        if receipt.task is not None and not receipt.task.done():
            receipt.task.cancel()
            tasks.append(receipt.task)
        if not self.pending and self.task is not None:
            self.task.cancel()
            tasks.append(self.task)
        self.wake.set()
        if tasks:
            joined = asyncio.gather(*tasks, return_exceptions=True)
            cancelled = False
            while not joined.done():
                try:
                    await asyncio.shield(joined)
                except asyncio.CancelledError:
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError
