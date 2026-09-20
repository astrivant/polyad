"""
Reconcile coalesced adaptation intent into ready, bounded subprocess compositions.
"""

from __future__ import annotations

import math
import threading
import time
from typing import TYPE_CHECKING

from opentelemetry.context import get_current

# Preserve existing import paths while keeping each exception defined centrally.
from polyad_sdk.exceptions.processes import _Aborted
from polyad_sdk.observability import Telemetry
from polyad_sdk.processes.models import PlanResult
from polyad_sdk.processes.process import ManagedProcess
from polyad_sdk.runtime.environment import env as sdk_environment

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from polyad_sdk.processes.models import ProcessPlan, ProcessSpec
    from polyad_sdk.symbiosis.models import Environment

__all__ = ("ProcessSupervisor",)


class ProcessSupervisor:
    """
    Execute approved plans on the application's supervisor thread with bounded overlap.

    propose() stores one latest desired profile and is safe for strategy callbacks.
    reconcile() owns readiness, commit, rollback and draining on its caller's
    thread. It never runs concurrently with another reconciliation or close().
    Unchanged live specifications are reused. Failed replacements leave the old
    composition committed; successfully committed replacements drain old workers.
    """

    def __init__(
        self,
        plans: Sequence[ProcessPlan],
        *,
        view: Callable[[], Environment],
        activate: Callable[[tuple[ManagedProcess, ...]], None],
        max_processes: int,
        telemetry: Telemetry | None = None,
        environ: Mapping[str, str] | None = None,
        cooldown_seconds: float = 0,
        startup_timeout: float = 10,
        drain_timeout: float = 10,
        stop_timeout: float = 5,
        poll_interval: float = 0.05,
    ) -> None:
        """
        Configure approved profiles and lifecycle limits without starting any workers.

        Args:
            plans (Sequence[ProcessPlan]): Uniquely named application-approved compositions.
            view (Callable[[], Environment]): Fresh service.view getter, evaluated before spawn and commit.
            activate (Callable[[tuple[ManagedProcess, ...]], None]): Bounded atomic routing switch; failure must retain old routes.
            max_processes (int): Peak owned child count, including old/new overlap and cleanup.
            telemetry (Telemetry | None): Shared application instrumentation; None uses global providers.
            environ (Mapping[str, str] | None): Child environment base; defaults to the SDK's startup snapshot.
            cooldown_seconds (float): Minimum interval between committed profile changes.
            startup_timeout (float): Total readiness window for a proposed composition.
            drain_timeout (float): Maximum shared draining window before terminating retired workers.
            stop_timeout (float): Termination grace per child, followed by forced cleanup.
            poll_interval (float): Interval between bounded readiness or drain checks.

        Raises:
            ValueError: Profiles or lifecycle limits are invalid.
            TypeError: A lifecycle callback is not callable.
        """
        from polyad_sdk.processes.models import ProcessPlan

        selected = tuple(plans)
        if not selected or any(not isinstance(plan, ProcessPlan) for plan in selected):
            raise ValueError("provide at least one approved ProcessPlan")
        self._plans = {plan.name: plan for plan in selected}
        if len(self._plans) != len(selected):
            raise ValueError("approved plan names must be unique")
        if type(max_processes) is not int or max_processes < 1:
            raise ValueError("max_processes must be a positive integer")
        for value in (cooldown_seconds, startup_timeout, drain_timeout, stop_timeout, poll_interval):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError("lifecycle durations must be finite nonnegative seconds")
        if poll_interval == 0 or startup_timeout == 0:
            raise ValueError("poll_interval and startup_timeout must be positive")
        if any(len(plan.processes) > max_processes for plan in selected):
            raise ValueError("a plan exceeds max_processes before accounting for overlap")
        if not callable(view) or not callable(activate):
            raise TypeError("view and activate must be callable")
        self._view, self._activate = view, activate
        self._limit = max_processes
        self.telemetry = telemetry if telemetry is not None else Telemetry()
        self._results = self.telemetry.meter.create_counter("polyad.sdk.plan.results", unit="{attempt}")
        self._environment = dict(sdk_environment if environ is None else environ)
        self._cooldown, self._startup, self._drain = cooldown_seconds, startup_timeout, drain_timeout
        self._stop_timeout, self._poll = stop_timeout, poll_interval

        # Proposal callbacks only update intent under _requests. The separate
        # execution lock gives one reconciliation exclusive lifecycle ownership.
        self._requests = threading.RLock()
        self._execution = threading.Lock()
        self._desired: str | None = None
        self._profile: str | None = None
        self._revision = 0
        self._proposal_context = get_current()
        self._closed = False
        self._active: tuple[ManagedProcess, ...] = ()
        self._owned: dict[int, ManagedProcess] = {}
        self._committed_at = float("-inf")

    @property
    def profile(self) -> str | None:
        """
        Read the last committed profile for threshold strategies.

        Returns:
            str | None: Committed plan name, or None before initial activation or after close.
        """
        with self._requests:
            return self._profile

    @property
    def active(self) -> tuple[ManagedProcess, ...]:
        """
        Read the committed process handles for routing and health inspection.

        Returns:
            tuple[ManagedProcess, ...]: Current committed roles; inspect returncode for unexpected exits.
        """
        with self._requests:
            return self._active

    def propose(self, profile: str) -> bool:
        """
        Replace pending intent, coalescing duplicates without starting work.

        Args:
            profile (str): Name of an approved plan.

        Returns:
            bool: True when intent changed; False when the same target was already requested.

        Raises:
            ValueError: The profile is not approved.
            RuntimeError: Shutdown has started.
        """
        with self._requests:
            if self._closed:
                raise RuntimeError("process supervisor is closed")
            if profile not in self._plans:
                raise ValueError("profile is not in the approved plans")
            if self._desired == profile:
                return False

            # Coalesce proposals into the newest revision. In-flight work checks
            # this revision before starting or committing replacement processes.
            self._desired = profile
            self._revision += 1
            self._proposal_context = get_current()
            return True

    def _admit(self, plan: ProcessPlan, revision: int) -> None:
        with self._requests:
            if self._closed or self._revision != revision:
                raise _Aborted(PlanResult(plan.name, "Superseded", "A newer target or shutdown replaced this proposal"))

        # Re-read admission evidence: topology and constraints may have changed
        # since the view that originally produced this proposal.
        current = self._view()
        if not current.available:
            raise _Aborted(PlanResult(plan.name, "Blocked", "Current topology does not admit a process change"))
        for guard in plan.guards:
            assessment = guard.evaluate(current)
            if not assessment.satisfied:
                raise _Aborted(PlanResult(plan.name, "Blocked", f"Constraint {guard.name}: {assessment.state}"))

    def _start(self, spec: ProcessSpec) -> ManagedProcess:
        with self.telemetry.operation("process.start", attributes={"process.role": spec.name}):
            environment = {**self._environment, **spec.environment}

            # Children belong to this launch span, not a stale parent inherited
            # from the supervisor's own startup environment.
            for key in ("TRACEPARENT", "TRACESTATE"):
                environment.pop(key, None)
            environment.update(self.telemetry.propagation_environment())
            process = ManagedProcess(spec, environment)
            self._owned[process.pid] = process
            self.telemetry.processes.add(1)
            return process

    def _stop(self, process: ManagedProcess) -> None:
        with self.telemetry.operation("process.stop", attributes={"process.role": process.spec.name}):
            process.stop(timeout=self._stop_timeout)
            if self._owned.pop(process.pid, None) is not None:
                self.telemetry.processes.add(-1)

    def _cleanup(self, processes: Sequence[ManagedProcess]) -> None:
        # Attempt every stop even if one fails; otherwise an early exception
        # could leave unrelated owned children running without a cleanup attempt.
        error: BaseException | None = None
        for process in processes:
            try:
                self._stop(process)
            except BaseException as failure:
                error = failure
        if error is not None:
            raise error

    def _retire(self, processes: Sequence[ManagedProcess]) -> None:
        pending = list(processes)
        deadline = time.monotonic() + self._drain
        try:
            while pending:
                for process in tuple(pending):
                    if process.returncode is not None or process.spec.drain(process):
                        self._stop(process)
                        pending.remove(process)
                if not pending or time.monotonic() >= deadline:
                    break
                time.sleep(min(self._poll, max(0, deadline - time.monotonic())))
        finally:
            self._cleanup(pending)

    def _apply(self, plan: ProcessPlan, revision: int) -> PlanResult:
        self._admit(plan, revision)
        self._cleanup([process for process in self._owned.values() if process.returncode is not None])

        # Reuse a worker only when its full specification still matches. Changed
        # roles need a ready replacement before application routing can switch.
        old = self.active
        reusable = {process.spec.name: process for process in old if process.returncode is None}
        kept = {spec.name: reusable[spec.name] for spec in plan.processes if spec.name in reusable and reusable[spec.name].spec == spec}
        additions = [spec for spec in plan.processes if spec.name not in kept]
        if self.profile == plan.name and not additions and len(kept) == len(old):
            return PlanResult(plan.name, "Unchanged", "The requested composition is already committed")
        if self.profile != plan.name and time.monotonic() - self._committed_at < self._cooldown:
            return PlanResult(plan.name, "Blocked", "Profile cooldown has not elapsed")

        # Count old and new workers together: this is the rolling transition's
        # peak population, not merely the smaller desired final population.
        if len(self._owned) + len(additions) > self._limit:
            return PlanResult(plan.name, "Blocked", "Old and proposed workers exceed the overlap limit")
        started: list[ManagedProcess] = []
        committed = False
        try:
            deadline = time.monotonic() + self._startup
            for spec in additions:
                self._admit(plan, revision)
                process = self._start(spec)
                started.append(process)
                kept[spec.name] = process

            # Wait until every proposed worker is alive and ready, checking
            # cancellation and admission again throughout the startup interval.
            proposed = tuple(kept[spec.name] for spec in plan.processes)
            while True:
                self._admit(plan, revision)
                if any(process.returncode is not None for process in proposed):
                    raise _Aborted(PlanResult(plan.name, "Failed", "A proposed worker exited before activation"))
                if all(process.spec.ready(process) for process in proposed):
                    if time.monotonic() > deadline:
                        raise _Aborted(PlanResult(plan.name, "Failed", "Worker readiness deadline elapsed"))
                    break
                if time.monotonic() >= deadline:
                    raise _Aborted(PlanResult(plan.name, "Failed", "Worker readiness deadline elapsed"))
                time.sleep(min(self._poll, max(0, deadline - time.monotonic())))
            self._admit(plan, revision)
            with self._requests:
                if self._closed or revision != self._revision:
                    raise _Aborted(PlanResult(plan.name, "Superseded", "A newer target replaced this proposal before activation"))

                # This callback commits routing. Only afterward can old workers
                # drain without leaving newly admitted work with no destination.
                self._activate(proposed)
                self._active, self._profile = proposed, plan.name
                self._committed_at = time.monotonic()
                committed = True
            self._retire([process for process in old if process not in proposed and process.pid in self._owned])
            return PlanResult(plan.name, "Applied", "Ready workers activated and previous workers retired")
        finally:
            # Roll back this attempt's new children, preserving the previous
            # active population when readiness or admission fails.
            if not committed:
                self._cleanup(started)

    def reconcile(self) -> PlanResult:
        """
        Apply the latest proposal, retaining the old composition until replacements are ready.

        Call this on the application's supervisor thread. Guards and proposal
        revision are rechecked during readiness and immediately before commit.
        Repeated calls can repair exited workers in the committed profile.

        Returns:
            PlanResult: Applied, blocked, superseded, failed, unchanged or idle attempt.

        Raises:
            RuntimeError: Another reconciliation or close is already executing.
        """

        # Lifecycle transactions cannot overlap, even when different application
        # threads try to reconcile the same coalesced intent.
        if not self._execution.acquire(blocking=False):
            raise RuntimeError("process reconciliation is already active")
        try:
            with self._requests:
                desired, revision = self._desired, self._revision
                parent = self._proposal_context
                if self._closed or desired is None:
                    return PlanResult(desired, "Idle", "No active proposal")
            try:
                with self.telemetry.operation("plan.reconcile", attributes={"plan.profile": desired}, parent=parent) as span:
                    try:
                        result = self._apply(self._plans[desired], revision)
                    except _Aborted as aborted:
                        result = aborted.result
                    span.set_attribute("plan.state", result.state)
            except Exception as error:
                result = PlanResult(desired, "Failed", f"Process plan failed: {type(error).__name__}")
            self._results.add(1, {"plan.profile": desired, "state": result.state})
            return result
        finally:
            self._execution.release()

    def close(self) -> None:
        """
        Withdraw routing, drain and reap all owned workers, including failed-attempt survivors.

        Run close outside lifecycle callbacks. It marks pending startup superseded
        before waiting for the executing reconciliation. Configure callbacks to
        return promptly so the application's shutdown deadline remains meaningful.

        Returns:
            None: Every owned child has completed cleanup; repeated closes are harmless.
        """
        with self._requests:
            self._closed = True
            self._revision += 1
        with self._execution:
            if not self._owned:
                if self._active:
                    self._activate(())
                with self._requests:
                    self._active, self._profile = (), None
                return
            try:
                self._activate(())
            finally:
                try:
                    self._retire(tuple(self._owned.values()))
                finally:
                    with self._requests:
                        self._active = tuple(process for process in self._active if process.pid in self._owned)
                        if not self._owned:
                            self._profile = None
