"""
Run bounded refreshed reconciliation workers and coalesce redundant watch notifications.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, TypeVar

from polyad.operator.coordination.settings import WorkGraphSettings

__all__ = (
    "Key",
    "RefreshQueue",
    "T",
    "batches",
    "reconciliation_workers",
)


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

type Key = tuple[str, str, str]
T = TypeVar("T")


def reconciliation_workers() -> int:
    """
    Load the bounded worker count shared by local refresh and remote delivery loops.

    Returns:
        int: Concurrent attempts; graph-family ownership still serializes related work.
    """
    return WorkGraphSettings.from_environment().reconciliation_workers


async def batches(items: Sequence[T], apply: Callable[[T], Awaitable[None]], limit: int) -> None:  # noqa: UP047 - pydocstyle 6.3 parser
    """
    Run bounded delivery batches and join every worker before leaving its parent task.

    Args:
        items (Sequence[T]): Independent deliveries, each retaining its own ownership checks.
        apply (Callable[[T], Awaitable[None]]): One delivery callback.
        limit (int): Positive upper bound on concurrent callbacks.

    Returns:
        None: Cancellation waits for callbacks, including their joined transport work.
    """
    if type(limit) is not int or not 1 <= limit <= 32:
        raise ValueError("delivery concurrency must be an integer between 1 and 32")

    async def dispatch(item: T) -> None:
        """
        Adapt callback awaitables to supervised coroutines.

        Args:
            item (T): One delivery item.

        Returns:
            None: Completion includes the callback's acknowledged effects.
        """
        await apply(item)

    # Each group is a completion barrier, including cancellation cleanup of sibling callbacks.
    for offset in range(0, len(items), limit):
        async with asyncio.TaskGroup() as group:
            for item in items[offset : offset + limit]:
                group.create_task(dispatch(item))


class RefreshQueue:
    """
    Keep one pending entry per resource; changes during execution get a later turn.
    """

    def __init__(self, reconcile: Callable[[Key], Awaitable[None]], *, workers: int | None = None) -> None:
        """
        Bind the callback, which must read authoritative state on every attempt.

        Args:
            reconcile (Callable[[Key], Awaitable[None]]): Async callback that refreshes and reconciles one resource key.
            workers (int | None): Concurrent attempts; omitted uses the configured process limit.
        """
        self.reconcile = reconcile
        self.workers = reconciliation_workers() if workers is None else workers
        if type(self.workers) is not int or not 1 <= self.workers <= 32:
            raise ValueError("reconciliation workers must be an integer between 1 and 32")
        self.queue: asyncio.Queue[Key] = asyncio.Queue()
        self.pending: dict[Key, list[asyncio.Future[None]]] = {}
        self.active: set[Key] = set()
        self.task: asyncio.Task[None] | None = None
        self.last_progress = time.monotonic()

    def start(self) -> None:
        """
        Start the worker supervisor on the operator event loop.

        Returns:
            None: No return value.
        """
        self.task = asyncio.create_task(self.run())

    async def submit(self, key: Key) -> None:
        """
        Wait for this submission's refreshed pass, including API acknowledgement.

        Args:
            key (Key): Resource kind, namespace and name to reconcile from fresh API state.

        Returns:
            None: No return value.
        """
        future = asyncio.get_running_loop().create_future()
        logger.debug("Refresh submitted kind=%s namespace=%s name=%s coalesced=%s", *key, key in self.pending)
        if key not in self.pending:
            self.pending[key] = []
            if key not in self.active:
                self.queue.put_nowait(key)
        self.pending[key].append(future)
        await future

    async def run(self) -> None:
        """
        Supervise workers so health checks and shutdown cover every consumer.

        Returns:
            None: An unexpected worker exit stops and joins the whole group.
        """
        async with asyncio.TaskGroup() as group:
            for _ in range(self.workers):
                group.create_task(self.consume())

    async def consume(self) -> None:
        """
        Process FIFO attempts; failures go to handlers for delayed retry.

        Returns:
            None: No return value.
        """
        while True:
            key = await self.queue.get()

            # Detach this turn's waiters; submissions during reconciliation need a subsequent read.
            waiters = self.pending.pop(key)
            self.active.add(key)
            logger.debug("Refresh dequeued kind=%s namespace=%s name=%s waiters=%s remaining=%s", *key, len(waiters), self.queue.qsize())
            try:
                await self.reconcile(key)
            except asyncio.CancelledError:
                for waiter in waiters:
                    waiter.cancel()
                raise
            except Exception as error:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.set_exception(error)
            else:
                for waiter in waiters:
                    if not waiter.done():
                        waiter.set_result(None)
            finally:
                self.active.remove(key)

                # Replay changes observed during the active pass without running this key twice.
                if key in self.pending:
                    self.queue.put_nowait(key)
                self.last_progress = time.monotonic()
                self.queue.task_done()

    async def stop(self) -> None:
        """
        Stop accepting ownership; unfinished desired state is replayed on resume.

        Returns:
            None: No return value.
        """
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for waiters in self.pending.values():
            for waiter in waiters:
                waiter.cancel()
        self.pending.clear()
