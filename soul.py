"""
Run Soul searching locally with real processes, TCP peers and adaptive workers.

Run it from the repository root; only Python's standard library is needed:

    python soul.py
    python soul.py --jobs 128 --work-seconds 0.02 --tick 0.02
    python soul.py --help

What runs
---------
S0, S1 and S2 are separate service processes. Each owns its child workers and
chooses when to change their execution profile. The root supervisor owns the
service network. A separate producer sends a warmup followed by a burst:

    main (root supervisor)
    +-- producer -------- IPC load --------> S0, S1, S2
    +-- S0
    |   +-- worker(s)
    +-- S1
    |   +-- worker(s)
    +-- S2
        +-- worker(s)

The branches above show process ownership. The diagrams below show data paths.

How the TCP network changes
---------------------------
Each service listens on an ephemeral localhost port. Arrows show who opens a
persistent peer connection; work results return through that same connection.

    1. Baseline: chain                         Cheeger = 1

    S0 ------> S1 ------> S2

    2. Sustained demand: add the S0 -> S2 edge  Cheeger = 2

    S0 ------> S1 ------> S2
    |                     ^
    +---------------------+

    3. Recovery: remove the added edge         Cheeger = 1

    S0 ------> S1 ------> S2

After at least two services switch to batch workers, the root admits the
triangle. Each newly opened TCP edge carries a square-computation job to a
child worker at its destination. The result is checked before that service
acknowledges the topology change. All three acknowledgements are required for
"topology_committed". These edges are actual sockets, not just diagram labels.

Once all services finish their accepted work and restore their interactive
profile, the root closes S0 -> S2 and restores the original chain. The original
chain connections stay open until the final shutdown.

How each service's processing tree changes
-----------------------------------------
Each service follows this cycle independently. Sx means any of S0, S1 or S2:

    Baseline              Under load              Recovered
    Sx                    Sx                      Sx
    +-- interactive       +-- batch-1              +-- interactive (new PID)
                          +-- batch-2
                          +-- batch-3

An interactive worker accepts one job per dispatch; a batch worker accepts up
to four. Both compute the same result. A simulated I/O delay per dispatch makes
batching's capacity benefit visible without an external dependency.

The handoff temporarily overlaps generations, within a four-worker ceiling:

    Scale up:  1 old interactive + 3 starting batch workers = 4 live children
    Recover:   3 old batch workers + 1 starting interactive = 4 live children

Replacements must acknowledge readiness before taking new work. Retiring
workers finish their accepted batches, receive a stop command and are joined.
The old generation continues serving while replacements start.

There is also a routing graph inside each service. D is its dispatch component
and C is its result collector; both live inside the service process:

    Baseline / recovered:                     Cheeger = 1

    D ------> interactive ------> C

    Under load:                               Cheeger = 1.5

             +--> batch-1 --+
    D -------+--> batch-2 --+------> C
             +--> batch-3 --+

These edges are worker command/result pipes. Each parallel branch connects the
same D and C vertices. Retiring workers drain accepted jobs outside the graph
used to admit new dispatches. Worker-routing and service-network Cheeger values
are computed independently by enumerating their cuts with direction ignored.

What drives the decisions
-------------------------
"search_soul" selects an approved profile from observed backlog and sustained
pressure. "service" checks constraints and enacts worker changes; "main" does
the same for the peer network. The defaults require three observations of at
least eight outstanding jobs before switching to batch workers. A cooldown
limits changes, and an empty backlog must remain quiet before recovery.

The fixed Cheeger floor stays at 1. Demand selects separate targets of 1.5 for
batch worker routing and 2 for the service triangle. Live process limits,
bounded batches, a 64-job pending queue and change budgets also constrain the
response. Cheeger measures structure; completed jobs measure useful work.

The producer's IPC load drives worker adaptation. The extra TCP jobs exercise
the admitted peer links. With defaults, all 288 producer jobs are verified,
along with the peer jobs. The root waits for baseline recovery, requests stop,
and joins the producer and services; each service joins its workers. Failures
and interruption enter cleanup without reporting success.

Reading the output
------------------
JSON lines include the process PID, service identity and monotonic timestamp:

    observed             backlog and completion deltas
    admitted / committed worker profile decisions and their Cheeger evidence
    worker_*             spawn, readiness, join and cleanup of real processes
    topology_admitted    proposed chain or triangle and its calculated Cheeger
    edge_opened / closed actual peer connection changes
    peer_work_completed  a child worker completed the job sent over a TCP edge
    topology_committed   every service acknowledged the new topology epoch
    baseline_restored    this service returned to one interactive worker
    success              the entire experiment finished and its tree was joined

Settings and CLI flags below expose load, timing and resource limits. See
"docs/workloads/local-soul-searching.md" for the walkthrough and the connection
to the proposed Natural Selection capability-placement and composition planner.
"""

from __future__ import annotations

import argparse
import math
import multiprocessing as mp
import os
import signal
import socket
import time
from collections import deque
from dataclasses import dataclass, fields
from select import select
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess
    from types import FrameType
    from typing import Any


@dataclass(frozen=True)
class Settings:
    """
    Set finite load, stabilization, process and runtime budgets.
    """

    jobs: int = 96
    work_seconds: float = 0.05
    tick: float = 0.05
    high_water: int = 8
    sustained: int = 3
    cooldown: float = 0.3
    idle_seconds: float = 0.6
    worker_limit: int = 4
    hard_minimum: float = 1.0
    timeout: float = 20.0


@dataclass(frozen=True)
class Profile:
    """
    Describe an approved execution role and its demand-selected expansion target.
    """

    role: str
    workers: int
    batch: int
    target: float


INTERACTIVE = Profile("interactive", 1, 1, 1.0)
BATCH = Profile("batch", 3, 4, 1.5)


@dataclass
class Child:
    """
    Track one execution incarnation, its pipe and its accepted batch.
    """

    process: BaseProcess
    pipe: Connection
    role: str
    ready: bool = False
    jobs: tuple[int, ...] = ()
    retiring: bool = False
    stopping: bool = False


def emit(event: str, **details: Any) -> None:
    """
    Write one atomic, structured lifecycle record.
    """
    import json

    record = {"event": event, "pid": os.getpid(), "time": round(time.monotonic(), 3), **details}
    os.write(1, (json.dumps(record, sort_keys=True) + "\n").encode())


def interrupt(signum: int, frame: FrameType | None) -> None:
    """
    Enter normal cleanup when a supervising process requests termination.
    """
    raise KeyboardInterrupt


def cheeger(workers: int = 0, *, links: list[tuple[int, int]] | None = None) -> float:
    """
    Enumerate all cuts of the worker routing graph or three-service peer graph.
    """
    if links is None and not 1 <= workers <= 6:
        raise ValueError("exact example calculations support one through six workers")
    size = 3 if links is not None else workers + 2
    edges = links if links is not None else [(end, worker) for end in (0, 1) for worker in range(2, size)]
    return min(
        sum(((cut >> a) & 1) != ((cut >> b) & 1) for a, b in edges) / min(cut.bit_count(), size - cut.bit_count())
        for cut in range(1, 1 << (size - 1))
    )


def worker(pipe: Connection, profile: Profile, settings: Settings) -> None:
    """
    Serve the selected capability until accepted work finishes and shutdown arrives.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        pipe.send(("ready", os.getpid()))
        while (jobs := pipe.recv()) is not None:
            if not 1 <= len(jobs) <= profile.batch:
                raise ValueError("batch exceeds the admitted capability")
            time.sleep(settings.work_seconds)
            pipe.send(("done", [(identity, value * value) for identity, value in jobs]))
    except EOFError:
        pass
    finally:
        pipe.close()


def producer(outputs: list[Connection], settings: Settings) -> None:
    """
    Warm all three services, then deliver a burst followed by an explicit end of input.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, interrupt)
    try:
        for identity in range(settings.jobs):
            for output in outputs:
                output.send((identity, identity + 1))
            if identity < 3:
                time.sleep(settings.work_seconds * 2)
        for output in outputs:
            output.send(None)
        emit("load_finished", offered_per_service=settings.jobs)
    finally:
        for output in outputs:
            output.close()


def search_soul(active: Profile, backlog: int, high: int, quiet: float, elapsed: float, settings: Settings) -> Profile:
    """
    Select an approved profile from sustained observations and a quiet-window hysteresis.
    """
    if elapsed < settings.cooldown:
        return active
    if active == INTERACTIVE and backlog >= settings.high_water and high >= settings.sustained:
        return BATCH
    if active == BATCH and backlog == 0 and quiet >= settings.idle_seconds:
        return INTERACTIVE
    return active


def service(name: str, incoming: Connection, control: Connection, settings: Settings) -> None:
    """
    Own a bounded process subtree and roll capabilities without dropping accepted jobs.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, interrupt)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.setblocking(False)
    port = listener.getsockname()[1]
    peers: dict[str, socket.socket] = {}
    inbound: list[socket.socket] = []
    replies: dict[int, socket.socket] = {}
    context = mp.get_context("spawn")
    children: dict[int, Child] = {}
    pending: deque[tuple[int, int]] = deque()
    accepted: set[int] = set()
    completed: set[int] = set()
    active = INTERACTIVE
    candidate: Profile | None = INTERACTIVE
    generation, changes, high, previous_backlog, previous_done = 0, 0, 0, 0, 0
    ending, returned, stopping = False, False, False
    last_change = last_busy = time.monotonic()
    deadline = last_change + settings.timeout
    try:
        while True:
            now = time.monotonic()
            if now > deadline:
                raise TimeoutError(f"{name}: deadline exceeded")
            if control.poll():
                command = control.recv()
                if command == "stop":
                    stopping = True
                else:
                    epoch, destinations = command
                    for peer in list(peers):
                        if peer not in destinations:
                            peers.pop(peer).close()
                            emit("edge_closed", service=name, target=peer, epoch=epoch)
                    for peer, address in destinations.items():
                        if peer in peers:
                            continue
                        connection = socket.create_connection(("127.0.0.1", address), timeout=5)
                        peers[peer] = connection
                        value = -(1 + int(name[-1]) * 3 + int(peer[-1]) + epoch * 10)
                        connection.sendall(f"{value}\n".encode())
                        with connection.makefile("rb") as stream:
                            result = int(stream.readline(128))
                        if result != value * value:
                            raise RuntimeError("peer returned an incorrect work result")
                        emit("edge_opened", service=name, target=peer, epoch=epoch, result=result)
                    control.send(("linked", name, epoch))
            if len(pending) < 64 and len(inbound) < 3 and select([listener], [], [], 0)[0]:
                connection, _ = listener.accept()
                connection.settimeout(5)
                inbound.append(connection)
                with connection.makefile("rb") as stream:
                    value = int(stream.readline(128))
                identity = value - 1
                if identity >= 0 or identity in accepted:
                    raise RuntimeError("invalid peer work identity")
                accepted.add(identity)
                replies[identity] = connection
                pending.append((identity, value))
            idle_links = [connection for connection in inbound if connection not in replies.values()]
            for connection in select(idle_links, [], [], 0)[0]:
                if connection.recv(1):
                    raise RuntimeError("unexpected data after the peer's work receipt")
                inbound.remove(connection)
                connection.close()
            for pid, child in list(children.items()):
                if child.pipe.poll() and not child.stopping:
                    kind, result = child.pipe.recv()
                    if kind == "ready":
                        child.ready = True
                        emit("worker_ready", service=name, worker=pid, role=child.role)
                    else:
                        if {identity for identity, _ in result} != set(child.jobs) or len(result) != len(child.jobs):
                            raise RuntimeError("completion does not match its accepted batch")
                        for identity, value in result:
                            if value != (identity + 1) ** 2 or identity in completed:
                                raise RuntimeError("wrong or duplicate result")
                            completed.add(identity)
                            if identity in replies:
                                replies.pop(identity).sendall(f"{value}\n".encode())
                                emit("peer_work_completed", service=name, job=identity, result=value)
                        child.jobs = ()
                if not child.process.is_alive():
                    child.process.join()
                    if not child.stopping or child.process.exitcode != 0 or child.jobs:
                        raise RuntimeError(f"worker {pid} exited before a clean drain")
                    child.pipe.close()
                    del children[pid]
                    emit("worker_joined", service=name, worker=pid, role=child.role)
            while not stopping and not ending and len(pending) < 64 and incoming.poll():
                job = incoming.recv()
                if job is None:
                    ending = True
                    break
                identity, value = job
                if identity in accepted or identity not in range(settings.jobs) or value != identity + 1:
                    raise RuntimeError("invalid or duplicate input")
                accepted.add(identity)
                pending.append(job)
            backlog = len(pending) + sum(len(child.jobs) for child in children.values())
            if backlog != previous_backlog or len(completed) != previous_done:
                emit(
                    "observed",
                    service=name,
                    backlog=backlog,
                    delta_backlog=backlog - previous_backlog,
                    completed=len(completed),
                    completed_delta=len(completed) - previous_done,
                )
            if backlog:
                last_busy = now
            high = high + 1 if backlog >= settings.high_water else 0
            steady = children and all(child.ready and not child.retiring for child in children.values()) and candidate is None
            if steady:
                proposal = search_soul(active, backlog, high, now - last_busy, now - last_change, settings)
                if proposal != active:
                    candidate = proposal
            if candidate is not None and not any(child.role == candidate.role and not child.retiring for child in children.values()):
                expansion = cheeger(candidate.workers)
                if (
                    changes >= 4
                    or len(children) + candidate.workers > settings.worker_limit
                    or expansion < max(settings.hard_minimum, candidate.target)
                ):
                    emit("blocked", service=name, role=candidate.role, live=len(children), worker_limit=settings.worker_limit)
                    raise RuntimeError("proposed profile violates the process, change or Cheeger budget")
                emit(
                    "admitted",
                    service=name,
                    role=candidate.role,
                    backlog=backlog,
                    delta_backlog=backlog - previous_backlog,
                    completed_delta=len(completed) - previous_done,
                    current_cheeger=cheeger(active.workers),
                    cheeger=expansion,
                    target=candidate.target,
                    hard_minimum=settings.hard_minimum,
                    generation=generation + 1,
                )
                for _ in range(candidate.workers):
                    parent, child_pipe = context.Pipe()
                    process = context.Process(target=worker, args=(child_pipe, candidate, settings), name=f"{name}-{candidate.role}")
                    process.start()
                    child_pipe.close()
                    assert process.pid is not None
                    children[process.pid] = Child(process, parent, candidate.role)
                    emit("worker_spawned", service=name, worker=process.pid, role=candidate.role, live=len(children))
            if candidate is not None:
                replacements = [child for child in children.values() if child.role == candidate.role and not child.retiring]
                if len(replacements) == candidate.workers and all(child.ready for child in replacements):
                    for child in children.values():
                        child.retiring = child.role != candidate.role
                    active, candidate = candidate, None
                    generation += 1
                    changes += generation > 1
                    high, last_change = 0, now
                    emit(
                        "committed",
                        service=name,
                        role=active.role,
                        workers=active.workers,
                        cheeger=cheeger(active.workers),
                        generation=generation,
                    )
                    control.send(("ready" if generation == 1 else active.role, name, port))
            for child in children.values():
                if (child.retiring or (stopping and not pending)) and not child.jobs and not child.stopping:
                    child.pipe.send(None)
                    child.stopping = True
                elif child.ready and child.role == active.role and not child.retiring and not child.stopping and not child.jobs and pending:
                    batch = [pending.popleft() for _ in range(min(active.batch, len(pending)))]
                    child.jobs = tuple(identity for identity, _ in batch)
                    child.pipe.send(batch)
            if ending and not backlog and steady and active == INTERACTIVE and not returned:
                if generation < 3 or len(accepted.intersection(range(settings.jobs))) != settings.jobs or completed != accepted:
                    raise RuntimeError("experiment did not adapt, restore and complete every job")
                returned = True
                emit("baseline_restored", service=name, completed=settings.jobs, workers=len(children))
                control.send(("restored", name, port))
            if stopping and not pending and not children:
                control.send(("stopped", name, port))
                break
            previous_backlog, previous_done = backlog, len(completed)
            time.sleep(settings.tick)
    finally:
        for child in children.values():
            if child.process.is_alive() and not child.stopping:
                try:
                    child.pipe.send(None)
                except (BrokenPipeError, OSError):
                    pass
        for child in children.values():
            child.process.join(timeout=2)
            if child.process.is_alive():
                child.process.terminate()
                child.process.join(timeout=2)
            if child.process.is_alive():
                child.process.kill()
                child.process.join()
            emit("worker_cleaned", service=name, worker=child.process.pid, exitcode=child.process.exitcode)
            child.pipe.close()
        for connection in [listener, *peers.values(), *inbound]:
            connection.close()
        incoming.close()
        control.close()


def main() -> None:
    """
    Run three services, admit their topology changes, then verify recovery and stop the tree.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for item in fields(Settings):
        parser.add_argument("--" + item.name.replace("_", "-"), type=type(item.default), default=item.default)
    settings = Settings(**vars(parser.parse_args()))
    if any(not math.isfinite(value) or value <= 0 for value in vars(settings).values()) or not 24 <= settings.jobs <= 2000:
        parser.error("use finite positive limits and 24 through 2000 jobs per service")
    if settings.worker_limit > 6 or settings.hard_minimum > cheeger(1):
        parser.error("worker-limit must be at most six; the initial graph must satisfy the hard minimum")
    signal.signal(signal.SIGTERM, interrupt)
    context = mp.get_context("spawn")
    processes: list[BaseProcess] = []
    controls: list[Connection] = []
    outputs: list[Connection] = []
    generator: BaseProcess | None = None
    ready: dict[str, int] = {}
    hot: set[str] = set()
    linked: set[str] = set()
    epoch, reported, topology = 0, 0, "starting"
    restored: set[str] = set()
    stopped: set[str] = set()
    try:
        for index in range(3):
            incoming, output = context.Pipe(duplex=False)
            parent, child = context.Pipe()
            process: BaseProcess = context.Process(
                target=service, args=(f"service-{index}", incoming, child, settings), name=f"service-{index}"
            )
            process.start()
            processes.append(process)
            controls.append(parent)
            outputs.append(output)
            incoming.close()
            child.close()
        deadline = time.monotonic() + settings.timeout
        while True:
            for index, control in enumerate(controls):
                if f"service-{index}" not in stopped and control.poll():
                    kind, name, value = control.recv()
                    if kind == "ready":
                        ready[name] = value
                    elif kind == "linked" and value == epoch:
                        linked.add(name)
                    elif kind in {"batch", "restored", "stopped"}:
                        {"batch": hot, "restored": restored, "stopped": stopped}[kind].add(name)
            next_topology = topology
            if len(ready) == 3 and topology == "starting":
                next_topology = "chain"
            elif len(hot) >= 2 and topology == "chain" and epoch == 1:
                next_topology = "triangle"
            elif len(restored) == 3 and topology == "triangle" and len(linked) == 3:
                next_topology = "chain"
            if next_topology != topology:
                edges = [(0, 1), (1, 2)] + ([(0, 2)] if next_topology == "triangle" else [])
                target = 2 if next_topology == "triangle" else 1
                if cheeger(links=edges) < max(settings.hard_minimum, target):
                    raise RuntimeError("peer topology violates its Cheeger constraints")
                topology, epoch = next_topology, epoch + 1
                linked.clear()
                emit("topology_admitted", topology=topology, epoch=epoch, edges=edges, cheeger=cheeger(links=edges), target=target)
                for index, control in enumerate(controls):
                    control.send((epoch, {f"service-{b}": ready[f"service-{b}"] for a, b in edges if a == index}))
            if len(linked) == 3 and reported != epoch:
                emit("topology_committed", topology=topology, epoch=epoch)
                reported = epoch
            if len(linked) == 3 and generator is None:
                generator = context.Process(target=producer, args=(outputs, settings), name="load-generator")
                generator.start()
                processes.append(generator)
                emit("load_started", producer=generator.pid, services=[process.pid for process in processes[:3]])
                for output in outputs:
                    output.close()
            if epoch == 3 and len(linked) == 3 and len(restored) == 3:
                for control in controls:
                    control.send("stop")
                restored.clear()
            if len(stopped) == 3:
                break
            if time.monotonic() > deadline or any(process.exitcode not in (None, 0) for process in processes):
                raise RuntimeError("experiment deadline or child failure; stopping the tree")
            time.sleep(settings.tick)
        for process in processes:
            process.join(timeout=3)
            if process.exitcode != 0:
                raise RuntimeError(f"{process.name} did not exit cleanly")
        emit("success", services=3, completed=3 * settings.jobs, topology=topology, all_joined=True)
    finally:
        for control in controls:
            try:
                control.send("stop")
            except (BrokenPipeError, OSError):
                pass
        for process in processes:
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join()
        for pipe in [*controls, *outputs]:
            pipe.close()


if __name__ == "__main__":
    main()
