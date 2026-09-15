"""Serialize refreshed reconciliation and coalesce redundant watch notifications."""

import asyncio
import time
from collections.abc import Awaitable, Callable

type Key = tuple[str, str, str]


class RefreshQueue:
    """Keep one FIFO entry per resource; changes during execution get another turn."""

    def __init__(self, reconcile: Callable[[Key], Awaitable[None]]) -> None:
        """Bind the callback, which must read authoritative state on every attempt."""
        self.reconcile = reconcile
        self.queue: asyncio.Queue[Key] = asyncio.Queue()
        self.pending: dict[Key, list[asyncio.Future[None]]] = {}
        self.task: asyncio.Task[None] | None = None
        self.last_progress = time.monotonic()

    def start(self) -> None:
        """Start a single consumer on the operator event loop."""
        self.task = asyncio.create_task(self.run())

    async def submit(self, key: Key) -> None:
        """Wait for this submission's refreshed pass, including API acknowledgement."""
        future = asyncio.get_running_loop().create_future()
        if key not in self.pending:
            self.pending[key] = []
            self.queue.put_nowait(key)
        self.pending[key].append(future)
        await future

    async def run(self) -> None:
        """Process FIFO attempts; failures go to handlers for delayed retry."""
        while True:
            key = await self.queue.get()
            waiters = self.pending.pop(key)
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
                self.last_progress = time.monotonic()
                self.queue.task_done()

    async def stop(self) -> None:
        """Stop accepting ownership; unfinished desired state is replayed on resume."""
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for waiters in self.pending.values():
            for waiter in waiters:
                waiter.cancel()
        self.pending.clear()
