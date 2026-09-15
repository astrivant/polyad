"""
Make your own pipeline of executions that need to occur easily in your project.

I will probably make this its own module at some point. It helps automate local runtime / benchmarking / graph regeneration.
"""

import json
import re
import time
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from contextlib import AbstractContextManager, nullcontext
from dataclasses import asdict, dataclass, field
from pathlib import Path
from subprocess import CompletedProcess
from typing import Protocol, TextIO

from polyad.graph.workloads import Statistics


class ProcessOwner(Protocol):
    """
    Application-supplied owner that retains a command and its descendants until joined.
    """

    def run(
        self,
        command: list[str],
        *,
        cwd: Path,
        env: dict[str, str],
        stdout: TextIO,
        stderr: TextIO,
        timeout: float | None,
    ) -> CompletedProcess[str]:
        """
        Execute one command while retaining ownership through communication failures.

        Args:
            command (list[str]): Direct executable arguments.
            cwd (Path): Working directory.
            env (dict[str, str]): Complete environment snapshot.
            stdout (TextIO): Operation log destination.
            stderr (TextIO): Operation error destination.
            timeout (float | None): Optional communication deadline.

        Returns:
            CompletedProcess[str]: Native command result.
        """
        ...

    def stop(self) -> None:
        """
        Prevent new children and stop and join owned process trees, safely on repeated calls.

        Returns:
            None: Ownership is released only after successful cleanup.
        """
        ...


@dataclass(frozen=True)
class Operation:
    """
    Declare one command, its prerequisites, and its scheduling requirements.

    Attributes:
        name (str): Unique filesystem-safe operation identifier.
        command (tuple[str, ...]): Direct executable invocation, without shell interpolation.
        requires (tuple[str, ...]): Operations that must complete before this one starts.
        exclusive (bool): Reserve the whole queue while this operation measures or publishes.
        allow_failure (bool): Retain nonzero exits for a required downstream verification operation.
        timeout (float | None): Optional command deadline, excluding owned-process cleanup.
        statistics (Statistics): Declared progress and cost estimates; unknown by default.
    """

    name: str
    command: tuple[str, ...]
    requires: tuple[str, ...] = ()
    exclusive: bool = False
    allow_failure: bool = False
    timeout: float | None = None
    statistics: Statistics = field(default_factory=Statistics)


class OperationQueue:
    """
    Own bounded command workers, dependency gates, and an atomic completion journal.
    """

    def __init__(
        self,
        operations: Sequence[Operation],
        *,
        workers: int,
        directory: Path,
        cwd: Path,
        environment: dict[str, str],
        owner_factory: Callable[[], ProcessOwner],
        notify: Callable[[str], None] = print,
        cancellation_scope: Callable[[], AbstractContextManager[object]] = nullcontext,
        critical_scope: Callable[[], AbstractContextManager[object]] = nullcontext,
    ) -> None:
        """
        Validate the entire inventory before starting any operation.

        Args:
            operations (Sequence[Operation]): Inventory in deterministic scheduling preference order.
            workers (int): Maximum simultaneous operations, independently of nested command workers.
            directory (Path): Journal and per-operation log directory.
            cwd (Path): Shared command working directory.
            environment (dict[str, str]): Explicit environment snapshot passed to each command.
            owner_factory (Callable[[], ProcessOwner]): Independent process owner supplied by the integrating application.
            notify (Callable[[str], None]): Coordinator-only progress sink, including plain CI logs.
            cancellation_scope (Callable[[], AbstractContextManager[object]]): Application cancellation-handler scope.
            critical_scope (Callable[[], AbstractContextManager[object]]): Scope deferring cancellation during ownership changes.
        """
        if type(workers) is not int or workers < 1:
            raise ValueError("workers must be a positive integer")
        self.operations = tuple(operations)
        self.workers = workers
        self.directory, self.cwd = directory, cwd
        self.environment = dict(environment)
        self.notify, self.owner_factory = notify, owner_factory
        self.cancellation_scope, self.critical_scope = cancellation_scope, critical_scope
        names = {item.name for item in operations}
        if len(names) != len(operations):
            raise ValueError("operation names must be unique")
        resolved: set[str] = set()
        for item in operations:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", item.name) or not item.command:
                raise ValueError(f"invalid operation: {item.name}")
            if set(item.requires) - names:
                raise ValueError(f"unknown prerequisites for {item.name}")
        while len(resolved) < len(names):
            ready = {item.name for item in operations if set(item.requires) <= resolved} - resolved
            if not ready:
                raise ValueError("operation prerequisites contain a cycle")
            resolved.update(ready)
        self.records: dict[str, dict[str, object]] = {
            item.name: {**asdict(item), "status": "pending", "exit_code": None} for item in self.operations
        }
        self.active: dict[Future[int], tuple[Operation, ProcessOwner]] = {}
        self.owners: dict[str, ProcessOwner] = {}

    def save(self) -> None:
        """
        Replace the journal atomically from the coordinator only.

        Returns:
            None: Every operation has one record, including blocked and cancelled work.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.directory / "operations.json.pending"
        temporary.write_text(json.dumps({"workers": self.workers, "operations": list(self.records.values())}, indent=2) + "\n")
        temporary.replace(self.directory / "operations.json")

    def execute(self, operation: Operation, owner: ProcessOwner) -> int:
        """
        Run one operation without sharing its process owner or log with another worker.

        Args:
            operation (Operation): Declared work whose dependencies are satisfied.
            owner (ProcessOwner): Owner registered before the worker can start a process.

        Returns:
            int: Native exit code; the coordinator applies the declared failure policy.
        """
        logs = self.directory / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / f"{operation.name}.log").open("w") as output:
            return owner.run(
                list(operation.command),
                cwd=self.cwd,
                env=self.environment,
                stdout=output,
                stderr=output,
                timeout=operation.timeout,
            ).returncode

    def run(self) -> dict[str, dict[str, object]]:
        """
        Process ready operations, stopping and joining all owned children on every exit path.

        Returns:
            dict[str, dict[str, object]]: Completed records, or a propagated failure after cleanup and journaling.
        """
        self.save()
        completed: set[str] = set()
        pending = list(self.operations)
        failures: list[BaseException] = []
        pool: ThreadPoolExecutor | None = None
        with self.cancellation_scope():
            try:
                with self.critical_scope():
                    pool = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="graph-worker")
                while pending or self.active:
                    exclusive = any(operation.exclusive for operation, _ in self.active.values())
                    for operation in tuple(pending):
                        if exclusive or len(self.active) >= self.workers:
                            break
                        if not set(operation.requires) <= completed:
                            continue
                        if operation.exclusive and self.active:
                            break
                        with self.critical_scope():
                            owner = self.owner_factory()
                            self.owners[operation.name] = owner
                            self.records[operation.name].update(status="running", started_epoch=time.time())
                            future = pool.submit(self.execute, operation, owner)
                            self.active[future] = operation, owner
                            pending.remove(operation)
                        self.save()
                        self.notify(f"[{len(completed)}/{len(self.operations)}] Starting {operation.name}")
                        exclusive = operation.exclusive
                    done, _ = wait(self.active, timeout=0.1, return_when=FIRST_COMPLETED)
                    for future in done:
                        operation, owner = self.active[future]
                        record = self.records[operation.name]
                        try:
                            code = future.result()
                            record["exit_code"] = code
                            owner.stop()
                            if code and not operation.allow_failure:
                                raise RuntimeError(
                                    f"Operation {operation.name} exited {code}; see {self.directory}/logs/{operation.name}.log"
                                )
                        except BaseException as exc:
                            record.update(status="failed", error=str(exc), finished_epoch=time.time())
                            raise
                        record.update(status="completed", finished_epoch=time.time())
                        completed.add(operation.name)
                        self.active.pop(future)
                        self.owners.pop(operation.name)
                        self.save()
                        self.notify(f"[{len(completed)}/{len(self.operations)}] Completed {operation.name} (exit {code})")
            except BaseException as exc:
                failures.append(exc)
            finally:
                # Stop every owner even if one cleanup fails; keep ownership until all threads join.
                try:
                    with self.critical_scope():
                        for name, owner in tuple(self.owners.items()):
                            try:
                                owner.stop()
                            except BaseException as exc:
                                failures.append(exc)
                            else:
                                self.owners.pop(name)
                            if self.records[name]["status"] == "running":
                                self.records[name].update(status="cancelled", finished_epoch=time.time())
                        if pool is not None:
                            try:
                                pool.shutdown(wait=True, cancel_futures=True)
                            except BaseException as exc:
                                failures.append(exc)
                        for operation in pending:
                            if self.records[operation.name]["status"] == "pending":
                                self.records[operation.name]["status"] = "blocked"
                        self.save()
                except BaseException as exc:
                    failures.append(exc)
        if len(failures) == 1:
            raise failures[0]
        if failures:
            raise BaseExceptionGroup("Operation queue and cleanup failed", failures)
        return self.records
