"""
Own a subprocess and its POSIX process group through readiness, draining and exit.
"""

from __future__ import annotations

import math
import os
import signal
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from polyad_sdk.processes.models import ProcessSpec


class ManagedProcess:
    """
    Expose the child identity used by application readiness and drain callbacks.

    Standard output and error inherit the parent workload's logging destinations.
    Standard input is closed. On POSIX, a new session contains the worker and its
    descendants for shutdown; other platforms terminate the direct child.

    Attributes:
        spec (ProcessSpec): Immutable construction and lifecycle description.
    """

    spec: ProcessSpec

    def __init__(self, spec: ProcessSpec, environment: Mapping[str, str]) -> None:
        """
        Start a directly owned child without a shell or unconsumed output pipes.

        Args:
            spec (ProcessSpec): Approved worker construction data.
            environment (Mapping[str, str]): Complete resolved environment including trace context.
        """
        self.spec = spec
        self._closed = False

        # Launch argv without a shell. A new POSIX session gives descendants a
        # process group that can be retired together with the immediate child.
        self._process = subprocess.Popen(
            spec.argv, cwd=spec.cwd, env=dict(environment), stdin=subprocess.DEVNULL, start_new_session=os.name == "posix"
        )

    @property
    def pid(self) -> int:
        """
        Return the concrete child identity for readiness and application IPC.

        Returns:
            int: Operating-system process identifier.
        """
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        """
        Poll and reap an exited direct child without waiting.

        Returns:
            int | None: Exit code, or None while the direct child remains alive.
        """
        return self._process.poll()

    def _signal(self, *, kill: bool) -> None:
        try:
            if os.name == "posix":
                os.killpg(self.pid, signal.SIGKILL if kill else signal.SIGTERM)
            elif self.returncode is None:
                self._process.kill() if kill else self._process.terminate()
        except ProcessLookupError:
            pass

    def stop(self, *, timeout: float = 5) -> None:
        """
        Terminate, escalate if needed and reap the directly owned worker.

        Args:
            timeout (float): Grace seconds after termination before forced cleanup.

        Returns:
            None: The direct child is reaped and its POSIX process group has been signaled for cleanup.
        """
        if self._closed:
            return
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout < 0:
            raise ValueError("timeout must be finite nonnegative seconds")

        # Offer graceful termination, then kill the group even if its immediate
        # child has exited: grandchildren may still require cleanup.
        self._signal(kill=False)
        try:
            self._process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass
        finally:
            self._signal(kill=True)
        self._process.wait(timeout=max(timeout, 1))
        self._closed = True
