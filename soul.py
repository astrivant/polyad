"""
Share real work over an adapting service network and measure the benefit.

Three services compute integer squares in child processes. S0 receives most of
an uneven load; S1 has less work and S2 has spare capacity. Services first use a
chain of TCP connections. Under sustained demand, the root can add S0 -> S2,
letting S0 delegate queued jobs directly to S2 while retaining responsibility
for their results. Each service also adjusts its own workers using Soul searching.

Run from the repository root with the local SDK installed:

    python -m pip install ./pkg/polyad-types ./pkg/polyad-sdk
    python soul.py                    # Compare chain and adaptive runs
    python soul.py --mode adaptive    # Just the changing topology
    python soul.py --mode chain       # Just the fixed topology
    python soul.py --jobs 768 --peer-window 12
    python soul.py --help

The default comparison runs both trials sequentially with fresh processes, the
same job IDs and values, the same worker profiles and the same resource limits.
Only permission to add the shortcut differs. Measurements include verified jobs,
completion time, mean/p95 latency and S0's accumulated backlog. A speedup above
1 means the adaptive trial finished sooner. Actual measurements are printed,
including slowdowns if process startup or scheduling outweighs the benefit.

Reading order
-------------
Start with main(), then follow the two supervisors:

    main -> parse_settings -> for each trial: SoulExperiment.run -> close
      |                             |
      |                             +-- start services and form a chain
      |                             +-- start the same uneven producer load
      |                             +-- optionally add S0 -> S2 under pressure
      |                             +-- verify all owned jobs, quiesce new work
      |                             +-- restore workers and chain, stop and join
      +-- compare the measured results

    service -> AdaptiveService.run
        receive commands / TCP / producer jobs -> measure -> SDK strategies
            -> roll workers -> dispatch locally -> share surplus -> drain

The reusable producer(), worker(), search_soul() and cheeger() functions also
support nature.py. Its own producer routing and capability selection are separate
from this demo's skewed_producer() and peer work-sharing protocol.

Process ownership and workload
------------------------------
With the defaults, --jobs 384 sends 384 jobs to S0, 128 to S1 and 32 to S2:

    main (root supervisor)
    +-- producer ----- IPC work ----> S0: 384 / S1: 128 / S2: 32
    +-- S0 -> its child worker(s)
    +-- S1 -> its child worker(s)
    +-- S2 -> its child worker(s)

Each trial verifies 544 unique jobs. Job IDs identify their original owner and
stay unchanged when another service executes them. Every job computes (ID+1)^2.
A service tracks an owned job until its final result is verified, whether the
result comes from a local worker or a TCP peer. Imported jobs execute locally;
they are not forwarded again. This keeps responsibility and duplicate checks
readable in a small example.

How the TCP network helps
-------------------------
Arrows carry jobs toward a compatible peer; results return on the same socket.
All services use the same square function. Each owns a bounded queue and can
advertise how many more jobs it can accept.

    Chain, Cheeger = 1             Adaptive triangle, Cheeger = 2

    S0 ------> S1 ------> S2       S0 ------> S1 ------> S2
    busy       less busy  spare    |                     ^
                                   +--- queued jobs -----+
                                       verified results return to S0

The fixed-chain trial can share S0 work with S1 and S1 work with S2. It cannot
send S0 jobs directly to S2 or relay them through S1. The adaptive trial adds
that direct path after S0 selects batch workers under sustained backlog. S0
keeps enough queued work for its local workers and delegates surplus through
eligible links. Newly reachable capacity can now complete the producer's load.

After all owners verify their jobs, the root announces quiesce: no new peer
assignments are needed. Services return to one worker. The shortcut stops new
assignments, waits for outstanding results and exchanges a drain acknowledgement
before closing. The root then acknowledges restoration of the original chain:

    S0 ------> S1 ------> S2       Final state before shutdown

The TCP protocol also supports removing a link while jobs are still in flight.
New sends stop immediately, while those jobs retain their source ownership and
must finish before the removal is acknowledged.

Which SDK strategies define this adaptation
-------------------------------------------
Work sharing is the existing neighbor-routing and work-distribution category:

    SDK topology baseline or delta
        -> WorkSharingStrategy(TopologyStrategy)
        -> remember eligible service names
        -> PeerAvailabilityStrategy + current service.view
        -> application checks credit, reserves its own jobs, sends a batch
        -> receiver checks its actual budget before accepting

WorkSharingStrategy specializes the SDK's TopologyStrategy; it only updates
routing intent. PeerAvailabilityStrategy checks admitted links, a completed
square-capability handshake, available capacity and drain state. FreshnessStrategy
also protects local profile changes. The local snapshot includes both workers
and connected service peers, so additions and removals follow SDK change delivery.
Application code owns sockets, job tracking and worker lifecycle.

An advertisement can become stale, especially if two senders see the same free
slots. The receiver rechecks a shared --peer-window budget and explicitly rejects
an entire batch that no longer fits. Only that rejection permits requeueing.
Wrong or duplicate results fail verification. A disconnect with an uncertain
execution outcome fails the trial and enters cleanup, retaining the ownership
record instead of blindly retrying potentially executed work.

How each service changes its processing tree
--------------------------------------------
Services use the same local worker policy and ceiling in both trials:

    Baseline              Sustained load            Recovered
    Sx                    Sx                        Sx
    +-- interactive       +-- batch-1               +-- interactive (new PID)
                          +-- batch-2
                          +-- batch-3

An interactive worker processes one job per dispatch. Each batch worker accepts
up to four. Both compute the same function; a simulated I/O delay per dispatch
makes batching useful. Three consecutive observations at or above eight
outstanding jobs select batch workers, subject to cooldown and resource checks.
The service stays ready for peer work until the root quiesces the completed load,
then a quiet interval permits recovery. A lightly loaded service may stay with
one worker throughout; S0 and S1 demonstrate replacement under the default load.

New workers must be ready before receiving jobs. Old workers finish their current
batches and exit. A four-worker limit includes this temporary overlap:

    Scale up:  1 old interactive + 3 starting batch workers = 4 live
    Recover:   3 old batch workers + 1 starting interactive = 4 live

The internal routing graph connects dispatch D and collect C through workers:

    One worker, Cheeger = 1        Three workers, Cheeger = 1.5

    D -> interactive -> C         D -> batch-1 -> C
                                  D -> batch-2 -> C
                                  D -> batch-3 -> C

Worker pipes and service TCP edges are measured separately, ignoring direction
for these exact Cheeger calculations. Both obey the fixed floor of 1. Demand
selects targets of 1.5 for workers and 2 for the triangle. Higher expansion
exposes another possible path; the job and latency measurements show its benefit.

Backpressure, measurements and shutdown
---------------------------------------
The application holds at most 64 queued-or-delegated jobs, plus batches already
executing locally. Each peer link has at most one batch outstanding; all incoming
peer jobs share the receiver's peer-window ceiling. Nonblocking TCP frames and
byte limits keep partial messages from blocking the service loop.

Completion time runs from producer launch to the last verified owned result.
Latency starts just before the producer's pipe write, so it includes waiting
behind backpressure. source_backlog_seconds integrates S0's observed unfinished
owned jobs, including delegated work; work still in the producer pipe is covered
by latency instead. Peak backlog may stay similar even when the queue drains
sooner. The comparison uses the same worker ceiling, not identical utilization:
the added path lets otherwise idle workers do useful work.

JSON lines explain what happened:

    trial_started           mode and per-service job counts
    observed                backlog and completion deltas
    admitted / committed    worker changes, limits and Cheeger values
    topology_committed      ready connections or fully drained removals
    work_delegated          original job IDs sent to a peer
    peer_batch_accepted     receiver budget checked and jobs accepted
    peer_backpressure       whole batch rejected before execution
    peer_work_completed     receiver's child worker produced the answer
    delegated_work_completed source verified that answer and released its slot
    edge_closed             connection drained with zero outstanding jobs
    baseline_restored       owned and imported jobs done; one local worker
    trial_verified          measured performance after every child is joined
    comparison              both measurements, speedup and backlog-area change
    success                 all selected trials finished and children exited

The root verifies every result and joins all services and producers; services
join their own workers. Errors and interruption run cleanup without reporting
success. See docs/workloads/local-soul-searching.md for the walkthrough and
nature.py for Natural Selection over these shared workers and policy functions.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing as mp
import os
import signal
import socket
import time
from collections import deque
from dataclasses import asdict, dataclass, field, fields
from select import select
from typing import TYPE_CHECKING

from polyad_sdk import AdaptiveService as SDKAdaptiveService
from polyad_sdk import Client, FreshnessStrategy, PeerAvailabilityStrategy, TopologyStrategy
from polyad_types import ServiceEndpoint
from polyad_types.events.envelope import Event

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess
    from types import FrameType
    from typing import Any, NoReturn

    from polyad_sdk import Change, ConstraintAssessment, Environment


@dataclass(frozen=True)
class Settings:
    """
    Set finite load, stabilization, process and runtime budgets.

    The command-line entry point validates these values before starting any
    processes. Timings are monotonic seconds; structural bounds are unweighted
    edge expansion. Worker limits include retiring and replacement generations.

    Attributes:
        jobs (int): Jobs sent to the busiest service; the other two get one
            third and one twelfth as many in this demo. producer(), reused by
            nature.py, still sends this many jobs to each supplied output.
        work_seconds (float): Simulated I/O overhead for each worker dispatch.
        tick (float): Delay between service and root observation cycles.
        high_water (int): Backlog that counts as a high-demand observation.
        sustained (int): Consecutive high-demand observations required to adapt.
        cooldown (float): Minimum elapsed time between worker-profile changes.
        idle_seconds (float): Quiet interval required to restore the baseline.
        worker_limit (int): Maximum live child workers owned by one service.
        hard_minimum (float): Fixed minimum Cheeger value for admitted graphs.
        timeout (float): Maximum duration of a service or root experiment loop.
        peer_window (int): Maximum unfinished jobs accepted from all peers together.
    """

    jobs: int = 384
    work_seconds: float = 0.05
    tick: float = 0.02
    high_water: int = 8
    sustained: int = 3
    cooldown: float = 0.3
    idle_seconds: float = 0.6
    worker_limit: int = 4
    hard_minimum: float = 1.0
    timeout: float = 20.0
    peer_window: int = 12


@dataclass(frozen=True)
class Profile:
    """
    Describe an approved execution role and its demand-selected expansion target.

    A profile changes concurrency and batch size while preserving the worker's
    computation. The demand target supplements the fixed structural floor.

    Attributes:
        role (str): Lifecycle identity, such as interactive or batch.
        workers (int): Number of ready workers required to commit this profile.
        batch (int): Maximum jobs admitted to one worker in a dispatch.
        target (float): Required expansion of the proposed worker-routing graph.
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

    The owning service alone updates this state. A retiring child receives no
    new work; a stopping child has already received its shutdown sentinel.

    Attributes:
        process (BaseProcess): Spawned child owned and joined by the service.
        pipe (Connection): Parent endpoint for commands and result messages.
        role (str): Execution profile under which the child was created.
        ready (bool): Whether the worker acknowledged its startup.
        jobs (tuple[int, ...]): Identities of the currently accepted batch.
        retiring (bool): Whether a replacement generation has excluded it.
        stopping (bool): Whether a stop command has been sent after draining.
    """

    process: BaseProcess
    pipe: Connection
    role: str
    ready: bool = False
    jobs: tuple[int, ...] = ()
    retiring: bool = False
    stopping: bool = False


@dataclass
class Peer:
    """
    Keep one TCP link and its unfinished jobs under the service's ownership.

    Messages are newline-delimited JSON with bounded buffers. Socket I/O never
    waits for a full message, so an incomplete frame cannot stop local workers.
    Advertised capacity is a hint; the receiver checks its budget again before
    accepting a batch. Only an explicit rejection allows the sender to requeue it.

    Attributes:
        connection (socket.socket): Nonblocking socket owned by this service.
        name (str): Other service's name, learned from hello on incoming links.
        epoch (int): Root revision that originally authorized this link.
        ready (bool): Whether the peer handshake has completed.
        credit (int): Most recently advertised free job slots at the receiver.
        advertised (int): Last capacity sent, to avoid repeating unchanged values.
        jobs (set[int]): Jobs sent on this link whose results are still owed.
        draining (bool): Whether new jobs are prohibited on this link.
        drain_sent (bool): Whether the sender has requested a drain acknowledgement.
        drained (bool): Whether the receiver acknowledged a complete drain.
        incoming (bytearray): Incomplete received frame.
        outgoing (bytearray): Encoded messages awaiting socket writes.
        closed (bool): Whether the other end closed the socket.
    """

    connection: socket.socket
    name: str = ""
    epoch: int = 0
    ready: bool = False
    credit: int = 0
    advertised: int = -1
    jobs: set[int] = field(default_factory=set)
    draining: bool = False
    drain_sent: bool = False
    drained: bool = False
    incoming: bytearray = field(default_factory=bytearray)
    outgoing: bytearray = field(default_factory=bytearray)
    closed: bool = False

    def send(self, kind: str, **payload: Any) -> None:
        """
        Queue a small protocol message without blocking the service loop.

        Args:
            kind (str): Message type, such as jobs, capacity or result.
            **payload (Any): JSON-serializable message fields.

        Returns:
            None: The encoded message awaits the next pump call.

        Raises:
            RuntimeError: A frame or the queued output exceeds its byte budget.
        """
        data = (json.dumps({"kind": kind, "epoch": self.epoch, **payload}) + "\n").encode()
        if len(data) > 8192 or len(self.outgoing) + len(data) > 65536:
            raise RuntimeError("peer output exceeds its bounded buffer")
        self.outgoing.extend(data)

    def pump(self) -> list[dict[str, Any]]:
        """
        Flush available output and read complete frames within bounded buffers.

        Returns:
            list[dict[str, Any]]: Complete messages in their original order.

        Raises:
            RuntimeError: A frame is oversized, malformed or truncated on close.
            OSError: TCP communication fails.
        """
        if self.outgoing:
            try:
                sent = self.connection.send(self.outgoing)
                del self.outgoing[:sent]
            except BlockingIOError:
                pass
        try:
            data = self.connection.recv(8192)
        except BlockingIOError:
            return []
        if not data:
            self.closed = True
            if self.incoming:
                raise RuntimeError("peer closed with an incomplete frame")
            return []
        self.incoming.extend(data)
        messages = []
        while b"\n" in self.incoming:
            frame, _, remaining = self.incoming.partition(b"\n")
            self.incoming = bytearray(remaining)
            if len(frame) > 8192:
                raise RuntimeError("peer frame exceeds 8 KiB")
            try:
                message = json.loads(frame)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RuntimeError("peer sent malformed JSON") from error
            if not isinstance(message, dict) or not isinstance(message.get("kind"), str):
                raise RuntimeError("peer message must be a JSON object with a kind")
            messages.append(message)
        if len(self.incoming) > 8192:
            raise RuntimeError("peer frame exceeds 8 KiB")
        return messages


class WorkSharingStrategy(TopologyStrategy):
    """
    Adapt the SDK's neighbor-routing strategy to this demo's service names.

    TopologyStrategy delivers connection additions, removals and unavailable
    views. This specialization remembers eligible service names; it does not
    send jobs. PeerAvailabilityStrategy and live receiver capacity gate each
    later send. Local child workers are separate candidates and are excluded.
    """

    def __init__(self, publish: Callable[[tuple[str, ...]], None]) -> None:
        """
        Bind the application's destination-list callback to topology changes.

        Args:
            publish (Callable[[tuple[str, ...]], None]): Save eligible peer names.
        """
        self._publish_peers = publish
        super().__init__(self.routes_changed)

    def routes_changed(self, change: Change, current: Environment) -> None:
        """
        Replace routing intent with service peers in the latest usable view.

        Args:
            change (Change): Baseline or topology delta delivered by the SDK.
            current (Environment): Freshly evaluated neighborhood and lifecycle.

        Returns:
            None: Unavailable views publish an empty destination list.
        """
        names = tuple(
            str(peer["node"]["name"])
            for peer in current.candidates
            if current.available and str(peer["node"]["name"]).startswith("service-")
        )
        self._publish_peers(names)


def emit(event: str, **details: Any) -> None:
    """
    Write one atomic, structured lifecycle record.

    Each demo record is encoded as a JSON line and written directly to stdout.
    The small records share a process ID and monotonic timestamp convention
    across the Soul and Nature supervisors and their child processes.

    Args:
        event (str): Machine-readable lifecycle or decision name.
        **details (Any): JSON-serializable observations and decision evidence.

    Returns:
        None: The record is written to stdout.

    Raises:
        TypeError: A supplied detail cannot be encoded as JSON.
        OSError: The output stream cannot accept the record.
    """
    record = {"event": event, "pid": os.getpid(), "time": round(time.monotonic(), 3), **details}
    os.write(1, (json.dumps(record, sort_keys=True) + "\n").encode())


def interrupt(signum: int, frame: FrameType | None) -> None:
    """
    Enter normal cleanup when a supervising process requests termination.

    Registered as a SIGTERM handler, this unwinds the active loop into its
    finally block so owned workers and communication endpoints can be closed.

    Args:
        signum (int): Signal number supplied by Python's signal dispatcher.
        frame (FrameType | None): Interrupted frame, when available.

    Returns:
        None: This handler always raises instead of returning normally.

    Raises:
        KeyboardInterrupt: Requests stack unwinding and owned-process cleanup.
    """
    raise KeyboardInterrupt


def cheeger(workers: int = 0, *, links: list[tuple[int, int]] | None = None) -> float:
    """
    Enumerate all cuts of the worker routing graph or three-service peer graph.

    Divide each cut's crossing-edge count by the size of its smaller side and
    return the minimum. Directions are ignored, and complementary cuts are
    examined once. The supported graphs are deliberately small for exact search.

    Args:
        workers (int): Worker count, from one through six, when links is None.
            The graph then has dispatch and collect vertices plus each worker.
        links (list[tuple[int, int]] | None): Optional distinct peer edges over
            vertices 0, 1 and 2. Supplying these replaces the worker graph.

    Returns:
        float: Exact unweighted edge expansion, represented as a float.

    Raises:
        ValueError: The requested worker count is outside the supported range.
    """
    if links is None and not 1 <= workers <= 6:
        raise ValueError("exact example calculations support one through six workers")
    size = 3 if links is not None else workers + 2
    edges = links if links is not None else [(end, worker) for end in (0, 1) for worker in range(2, size)]
    return min(
        sum(((cut >> a) & 1) != ((cut >> b) & 1) for a, b in edges) / min(cut.bit_count(), size - cut.bit_count())
        for cut in range(1, 1 << (size - 1))
    )


def worker(pipe: Connection, profile: Profile, settings: Settings, compute: Callable[[int], int] | None = None) -> None:
    """
    Serve the selected capability until accepted work finishes and shutdown arrives.

    Acknowledge readiness, then accept batches of identity/value pairs. Each
    batch incurs the configured simulated delay before results are returned.
    A None command ends the loop after preceding batches have completed.
    Nature supplies its selected computation through the same worker protocol.

    Args:
        pipe (Connection): Child endpoint used for commands and acknowledgements.
        profile (Profile): Approved role and maximum batch size.
        settings (Settings): Runtime settings, including per-dispatch delay.
        compute (Callable[[int], int] | None): Optional pure, spawn-importable
            capability function. None selects the original square operation.

    Returns:
        None: The worker has stopped and closed its communication endpoint.

    Raises:
        ValueError: An admitted batch is empty or exceeds the profile's limit.
        OSError: A result cannot be delivered to the supervising service.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        pipe.send(("ready", os.getpid()))
        while (jobs := pipe.recv()) is not None:
            if not 1 <= len(jobs) <= profile.batch:
                raise ValueError("batch exceeds the admitted capability")
            time.sleep(settings.work_seconds)
            pipe.send(("done", [(identity, compute(value) if compute is not None else value * value) for identity, value in jobs]))
    except EOFError:
        pass
    finally:
        pipe.close()


def producer(outputs: list[Connection], settings: Settings) -> None:
    """
    Warm each destination, then deliver a burst and an explicit end of input.

    Emit identities 0 through jobs - 1 with values identity + 1. The first three
    identities are spaced apart; the remainder create sustained pressure.
    Nature passes its parent's routing pipe. Soul's topology comparison uses
    skewed_producer() instead. Blocking OS pipes backpressure this finite producer.

    Args:
        outputs (list[Connection]): Writable endpoints receiving identical loads.
        settings (Settings): Number of jobs per endpoint and warmup timing.

    Returns:
        None: Every destination received its final None sentinel and was closed.

    Raises:
        OSError: A consumer disconnects while the producer is sending work.
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


def load_counts(settings: Settings) -> tuple[int, int, int]:
    """
    Give S0 most of the work and leave spare capacity at S2.

    Args:
        settings (Settings): Job count for S0.

    Returns:
        tuple[int, int, int]: Identical per-service loads for both comparison runs.
    """
    return settings.jobs, settings.jobs // 3, max(3, settings.jobs // 12)


def skewed_producer(outputs: list[Connection], settings: Settings) -> None:
    """
    Send the comparison workload with unique IDs and end-to-end start timestamps.

    An ID encodes its original service as ID divided by settings.jobs. Every
    value is ID plus one, so both the sender and receiver can verify results.
    Timestamps precede pipe writes, including producer backpressure in latency.

    Args:
        outputs (list[Connection]): Three producer pipes in service order.
        settings (Settings): Shared load and warmup timing for both trials.

    Returns:
        None: Each service received its finite load and end-of-input marker.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, interrupt)
    counts = load_counts(settings)
    try:
        for offset in range(max(counts)):
            for index, (output, count) in enumerate(zip(outputs, counts, strict=True)):
                if offset < count:
                    identity = index * settings.jobs + offset
                    output.send((identity, identity + 1, time.monotonic()))
                if offset == count - 1:
                    output.send(None)
            if offset < 3:
                time.sleep(settings.work_seconds * 2)
        emit("load_finished", counts=counts, total=sum(counts))
    finally:
        for output in outputs:
            output.close()


@dataclass
class LoadMetrics:
    """
    Measure only jobs originally assigned to this service, wherever they execute.

    Attributes:
        offered (dict[int, float]): Producer timestamps retained until final verification.
        latencies (list[float]): End-to-end seconds for verified owned jobs.
        finished (float): Time of the last owned completion.
        previous_time (float): Last backlog observation, or zero before the first.
        previous_backlog (int): Owned unfinished jobs at that observation.
        backlog_seconds (float): Integral of observed owned backlog over time.
        peak_backlog (int): Largest observed owned backlog.
        sent (dict[str, int]): Jobs delegated to each peer, excluding rejected batches.
        remote_executed (int): Peer-owned jobs completed by local workers.
    """

    offered: dict[int, float] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    finished: float = 0
    previous_time: float = 0
    previous_backlog: int = 0
    backlog_seconds: float = 0
    peak_backlog: int = 0
    sent: dict[str, int] = field(default_factory=dict)
    remote_executed: int = 0

    def observe(self, now: float) -> None:
        """
        Integrate unfinished owned work, including work delegated to peers.

        Args:
            now (float): Current monotonic time.

        Returns:
            None: Backlog area and peak are updated without double-counting peer jobs.
        """
        backlog = len(self.offered) - len(self.latencies)
        if self.previous_time:
            self.backlog_seconds += self.previous_backlog * (now - self.previous_time)
        self.previous_time, self.previous_backlog = now, backlog
        self.peak_backlog = max(self.peak_backlog, backlog)

    def complete(self, identity: int) -> None:
        """
        Record one verified result using the original producer timestamp.

        Args:
            identity (int): Owned job whose completion was checked by the service.

        Returns:
            None: Completion latency and the last completion time are recorded.
        """
        self.finished = time.monotonic()
        self.latencies.append(self.finished - self.offered[identity])


def search_soul(active: Profile, backlog: int, high: int, quiet: float, elapsed: float, settings: Settings) -> Profile:
    """
    Select an approved profile from sustained observations and a quiet-window hysteresis.

    This pure policy makes no process or routing changes. Cooldown is checked
    first, sustained pressure selects batching, and a drained quiet interval
    restores interactive processing. Admission remains the supervisor's job.

    Args:
        active (Profile): Currently committed interactive or batch profile.
        backlog (int): Accepted jobs that have not completed.
        high (int): Consecutive observations at or above the high-water mark.
        quiet (float): Seconds since the most recent nonempty backlog.
        elapsed (float): Seconds since the last committed profile change.
        settings (Settings): Cooldown, high-water, sustained and idle thresholds.

    Returns:
        Profile: The current profile or the approved proposed replacement.
    """
    if elapsed < settings.cooldown:
        return active
    if active == INTERACTIVE and backlog >= settings.high_water and high >= settings.sustained:
        return BATCH
    if active == BATCH and backlog == 0 and quiet >= settings.idle_seconds:
        return INTERACTIVE
    return active


@dataclass(frozen=True)
class Observation:
    """
    Capture one consistent view of pressure and progress for a policy decision.

    The supervisor observes once per cycle. Policy and admission use the same
    snapshot, keeping their evidence understandable even while workers run.

    Attributes:
        time (float): Monotonic time at which the observation was taken.
        backlog (int): Accepted jobs waiting, executing locally or delegated to peers.
        delta_backlog (int): Backlog change since the preceding observation.
        completed (int): Total verified jobs, including TCP peer work.
        completed_delta (int): Jobs completed since the preceding observation.
        high (int): Consecutive observations at or above the high-water mark.
        quiet (float): Seconds since a nonempty backlog was observed.
        elapsed (float): Seconds since the last committed worker profile.
    """

    time: float
    backlog: int
    delta_backlog: int
    completed: int
    completed_delta: int
    high: int
    quiet: float
    elapsed: float


class LocalObservations(Client):
    """
    Supply process-local topology through the SDK's snapshot client interface.

    This demo adapter reads the supervised worker tree directly. Operator-backed
    applications use the ordinary SDK Client with their authorized events URL.
    All HTTP operations are disabled here; local observations enter dispatch().

    Attributes:
        snapshot (Callable[[], dict[str, Any]]): Reader for this service's live tree.
    """

    snapshot: Callable[[], dict[str, Any]]

    def __init__(self, snapshot: Callable[[], dict[str, Any]]) -> None:
        """
        Bind a local reader without making network calls.

        Args:
            snapshot (Callable[[], dict[str, Any]]): Current worker-neighborhood reader.
        """
        super().__init__("http://local.invalid", None)
        self.snapshot = snapshot

    def topology(self, **selection: Any) -> dict[str, Any]:
        """
        Read the bound neighborhood for SDK identity validation and delta delivery.

        Args:
            **selection (Any): SDK selectors; the base validates the returned
                graph incarnation and node against its configured identity.

        Returns:
            dict[str, Any]: Fresh local snapshot in the SDK's neighborhood format.
        """
        return self.snapshot()

    def _open(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        headers: dict[str, str] | None = None,
        endpoint: tuple[str, int] | None = None,
    ) -> NoReturn:
        raise RuntimeError("the local observation adapter does not make HTTP requests")


class AdaptiveService(SDKAdaptiveService):
    """
    Demonstrate an application that owns its work, connections and worker lifecycle.

    Start reading at run(): each named step has one responsibility. Producer and
    peer adapters share the same work ledger. The SDK delivers observations to
    adapt(), which feeds the pure policy through propose_profile();
    admission checks its proposal before readiness allows a profile commit.
    Dispatch and completion preserve accepted work while generations overlap.

    The square computation lives in worker(). Application-specific input and
    result contracts live in receive_producer_work(), accept_peer_work() and
    complete_batch(). Change those contracts together when adapting this example
    to another application. Keep process ownership and lifecycle checks intact.

    Attributes:
        name (str): Service identity used in root messages and lifecycle logs.
        incoming (Connection): Producer's read-only work channel.
        control (Connection): Duplex root control and acknowledgement channel.
        runtime (Settings): Application load, policy and resource limits,
            separate from the inherited SDK observation settings.
        sequence (int): Last locally published observation's replay position.
        profile_available (bool): Latest strategy assessment permitting consideration of a profile change.
        process_context (mp.context.SpawnContext): Context used to create child workers.
        listener (socket.socket | None): Owned TCP listener after startup.
        port (int): Listener port announced to the root after worker readiness.
        peers (dict[str, Peer]): Outbound, root-admitted TCP connections.
        inbound (list[Peer]): Accepted incoming peer connections.
        replies (dict[int, Peer]): Peer jobs awaiting result delivery.
        delegated (dict[int, Peer]): Owned jobs awaiting verified results from peers.
        routes (tuple[str, ...]): Eligible service names selected by WorkSharingStrategy.
        peer_guard (PeerAvailabilityStrategy): SDK check for a usable downstream service.
        sharing_available (bool): Most recently published peer guard assessment.
        sources (set[str]): Incoming service identities authorized by the root.
        link_epoch (int): Latest root topology revision to acknowledge.
        link_pending (bool): Whether that revision still needs ready or drained links.
        quiescing (bool): Root confirmation that all producer jobs have completed.
        done_sent (bool): Whether owned-job completion was reported to the root.
        metrics (LoadMetrics): End-to-end measurements for this service's owned jobs.
        children (dict[int, Child]): All owned worker generations, indexed by PID.
        pending (deque[tuple[int, int]]): Bounded queue of undispatched work.
        accepted (set[int]): Every admitted producer and peer job identity.
        completed (set[int]): Every verified, completed identity.
        active (Profile): Worker profile currently eligible for dispatch.
        candidate (Profile | None): Proposed profile awaiting admission or readiness.
        generation (int): Number of committed worker generations.
        changes (int): Committed profile changes after the initial generation.
        high (int): Consecutive observations of high demand.
        previous_backlog (int): Backlog reported by the last observation.
        previous_done (int): Completion count reported by the last observation.
        ending (bool): Whether the producer has sent its end-of-input sentinel.
        returned (bool): Whether complete baseline recovery was acknowledged.
        stopping (bool): Whether the root has revoked new work admissions.
        last_change (float): Monotonic timestamp of the last profile commit.
        last_busy (float): Monotonic timestamp of the last nonempty observation.
        deadline (float): Absolute experiment deadline for this service.
    """

    name: str
    incoming: Connection
    control: Connection
    runtime: Settings
    sequence: int
    profile_available: bool
    process_context: mp.context.SpawnContext
    listener: socket.socket | None
    port: int
    peers: dict[str, Peer]
    inbound: list[Peer]
    replies: dict[int, Peer]
    delegated: dict[int, Peer]
    routes: tuple[str, ...]
    peer_guard: PeerAvailabilityStrategy
    sharing_available: bool
    sources: set[str]
    link_epoch: int
    link_pending: bool
    quiescing: bool
    done_sent: bool
    metrics: LoadMetrics
    children: dict[int, Child]
    pending: deque[tuple[int, int]]
    accepted: set[int]
    completed: set[int]
    active: Profile
    candidate: Profile | None
    generation: int
    changes: int
    high: int
    previous_backlog: int
    previous_done: int
    ending: bool
    returned: bool
    stopping: bool
    last_change: float
    last_busy: float
    deadline: float

    def __init__(self, name: str, incoming: Connection, control: Connection, settings: Settings) -> None:
        """
        Allocate supervision state without starting workers or binding sockets.

        The process entry point owns the finally block around run() and close().
        Deferring I/O to run() keeps partial startup inside that cleanup boundary.

        Args:
            name (str): Service name ending in its peer vertex index, 0 through 2.
            incoming (Connection): Producer endpoint transferred to this owner.
            control (Connection): Root endpoint transferred to this owner.
            settings (Settings): Validated runtime, policy and admission limits.
        """
        self.name = name
        self.incoming = incoming
        self.control = control
        self.runtime = settings
        self.sequence = 0
        self.profile_available = False
        self.process_context = mp.get_context("spawn")
        self.listener = None
        self.port = 0
        self.peers = {}
        self.inbound = []
        self.replies = {}
        self.delegated = {}
        self.routes = ()
        self.sharing_available = False
        self.sources = set()
        self.link_epoch = 0
        self.link_pending = False
        self.quiescing = False
        self.done_sent = False
        self.metrics = LoadMetrics()
        self.children = {}
        self.pending = deque()
        self.accepted = set()
        self.completed = set()
        self.active = INTERACTIVE
        self.candidate = INTERACTIVE
        self.generation = 0
        self.changes = 0
        self.high = 0
        self.previous_backlog = 0
        self.previous_done = 0
        self.ending = False
        self.returned = False
        self.stopping = False
        self.last_change = self.last_busy = time.monotonic()
        self.deadline = self.last_change + settings.timeout
        self.peer_guard = PeerAvailabilityStrategy("peer-work-capacity", self.observe_peers, usable=self.usable_peer)
        super().__init__(
            ServiceEndpoint("", "local", "Graph", name, f"local-{os.getpid()}-{name}", "dispatch"),
            LocalObservations(self.neighborhood),
            strategies=(
                FreshnessStrategy("profile-admission", self.observe_freshness),
                WorkSharingStrategy(self.remember_routes),
                self.peer_guard,
            ),
        )

    def run(self) -> None:
        """
        Coordinate the readable receive, observe, adapt, dispatch and drain cycle.

        Reaping happens before admission so live-process counts stay current.
        Policy proposals, budget admission and readiness commits are distinct
        steps. A stop request ends new admissions while accepted work drains.

        Returns:
            None: The root requested stop and all accepted work and workers drained.

        Raises:
            TimeoutError: The service exceeds its experiment deadline.
            RuntimeError: An application, peer, worker or admission contract fails.
            OSError: A listener, worker or control connection fails.
        """
        self.open_listener()
        while True:
            if time.monotonic() > self.deadline:
                raise TimeoutError(f"{self.name}: deadline exceeded")

            self.receive_control()
            self.accept_peer_work()
            self.poll_peers()
            self.collect_worker_results()
            self.reap_workers()
            self.receive_producer_work()

            observation = self.observe(time.monotonic())
            self.publish_observation(observation)
            self.admit_profile(observation)
            self.commit_profile(observation.time)
            self.dispatch_work()
            self.share_work()
            self.maintain_links()
            self.report_completion()
            self.report_recovery(observation)

            if self.finish_if_stopped():
                return
            time.sleep(self.runtime.tick)

    def stop(self) -> None:
        """
        Extend SDK shutdown to revoke local admissions and drain accepted work.

        The root control channel and application callers share this entry point.
        run() continues collecting results and joining workers until the owned
        processing tree is empty, then acknowledges shutdown to the root.

        Returns:
            None: Graceful drain is requested; run() completes it asynchronously.
        """
        super().stop()
        self.stopping = True
        for peer in self.peers.values():
            peer.draining = True

    def neighborhood(self) -> dict[str, Any]:
        """
        Describe the local dispatcher and its currently eligible worker neighbors.

        Process identities and readiness come from the owned child ledger. This
        mirrors the SDK snapshot format locally; no Kubernetes objects are created.
        Retiring children remain owned and drain outside the dispatch neighborhood.

        Returns:
            dict[str, Any]: Fresh dispatcher snapshot with ready worker candidates.
        """
        identity = self.identity
        return {
            "graph": {"kind": identity.kind, "namespace": identity.namespace, "name": identity.graph, "uid": identity.graphUid},
            "cursor": f"{self.sequence}-0",
            "revision": f"{self.link_epoch}:{self.generation}",
            "observedAt": time.time(),
            "valid": True,
            "terminating": self.stopping,
            "templateOnly": False,
            "node": {"name": identity.node, "kind": "Process", "ref": self.name, "desired": True, "requires": [], "executions": []},
            "incoming": [],
            "dependencies": [],
            "dependents": [],
            "outgoing": [
                {
                    "node": {
                        "name": f"worker-{pid}",
                        "kind": "Process",
                        "ref": child.role,
                        "desired": True,
                        "requires": [],
                        "executions": [{"kind": "Process", "name": f"worker-{pid}", "uid": f"process-{pid}", "terminating": False}],
                    },
                    "ports": [],
                }
                for pid, child in self.children.items()
                if child.ready and not child.retiring and not child.stopping and child.role == self.active.role
            ]
            + [
                {
                    "node": {
                        "name": name,
                        "kind": "Process",
                        "ref": "square",
                        "desired": True,
                        "requires": [],
                        "executions": [{"kind": "Process", "name": name, "uid": name, "terminating": False}],
                    },
                    "ports": [],
                }
                for name, peer in self.peers.items()
                if peer.ready and not peer.draining
            ],
        }

    def remember_routes(self, names: tuple[str, ...]) -> None:
        """
        Save the SDK topology strategy's current destination choices.

        Args:
            names (tuple[str, ...]): Ready peer names, excluding local workers.

        Returns:
            None: Subsequent delegation uses only these observed destinations.
        """
        self.routes = names

    def observe_peers(self, assessment: ConstraintAssessment) -> None:
        """
        Retain the SDK's capacity-aware peer assessment for routing.

        Args:
            assessment (ConstraintAssessment): Whether an eligible receiver has room.

        Returns:
            None: New delegation is gated by the latest assessment.
        """
        self.sharing_available = assessment.satisfied

    def usable_peer(self, candidate: Mapping[str, Any]) -> bool:
        """
        Check root permission, transport readiness and advertised receiver capacity.

        Args:
            candidate (Mapping[str, Any]): One node from the SDK's current neighborhood.

        Returns:
            bool: True for an admitted TCP peer that can accept a bounded batch.
        """
        peer = self.peers.get(str(candidate.get("node", {}).get("name", "")))
        return bool(peer and peer.ready and not peer.draining and not peer.jobs and peer.credit > 0)

    def publish_observation(self, observation: Observation) -> None:
        """
        Deliver local evidence through the SDK's normal snapshot and event pipeline.

        refresh() establishes or updates the worker neighborhood. dispatch()
        validates the event, derives immutable deltas and calls adapt() before
        additional hooks and cursor advancement. No policy callback is bypassed.

        Args:
            observation (Observation): This cycle's measured pressure and progress.

        Returns:
            None: The SDK delivered this observation and advanced its cursor.
        """
        self.refresh()
        self.sequence += 1
        identity = self.identity
        self.dispatch(
            Event(
                f"{self.sequence}-0",
                "graph",
                {
                    "kind": identity.kind,
                    "namespace": identity.namespace,
                    "name": identity.graph,
                    "uid": identity.graphUid,
                    "apiVersion": "polyad.io/v1alpha1",
                    "resourceVersion": str(self.sequence),
                    "generation": self.generation,
                    "type": "observation",
                    "owners": [],
                    "ancestry": [],
                    "audit": {},
                    "status": {},
                    "resources": asdict(observation),
                },
            )
        )

    def observe_freshness(self, assessment: ConstraintAssessment) -> None:
        """
        Retain the configured strategy's assessment for the application adaptation step.

        Args:
            assessment (ConstraintAssessment): Current topology freshness and lifecycle assessment.

        Returns:
            None: The following adapt() call can consider fresh profile proposals.
        """
        self.profile_available = assessment.satisfied

    def adapt(self, change: Change) -> None:
        """
        Translate SDK pressure deltas into a bounded application profile proposal.

        The first topology baseline has no metrics, so it leaves the initial
        interactive profile intact. Subsequent resource deltas include sustained
        pressure and elapsed quiet/cooldown windows even when backlog is steady.
        Recheck live freshness before acting. Admission and worker startup happen
        afterward in run(), keeping the callback bounded and safe to retry.

        Args:
            change (Change): SDK baseline or immutable neighborhood/metric delta.

        Returns:
            None: Fresh resource evidence may update the candidate profile.
        """
        if not self.profile_available or not change.after.available or not self.view.available:
            return
        if not change.baseline and not change.matching("resources"):
            return
        if (resources := change.after.resources) is not None:
            self.propose_profile(Observation(**resources))

    def open_listener(self) -> None:
        """
        Bind the owned peer listener on an ephemeral localhost port.

        Store ownership before binding so close() can release the socket even
        if startup fails. Announcing the port waits for worker readiness.

        Returns:
            None: The listener is ready to accept root-admitted peer connections.

        Raises:
            OSError: The listener cannot bind or begin listening.
        """
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.listener.setblocking(False)
        self.port = self.listener.getsockname()[1]

    def receive_control(self) -> None:
        """
        Apply root-approved peer changes without abandoning unfinished jobs.

        A removed link stops accepting assignments, finishes outstanding results
        and exchanges a drain acknowledgement before closing. The root receives
        linked only after every addition is ready and every removal is drained.

        Returns:
            None: One available topology, quiesce or stop command is processed.

        Raises:
            RuntimeError: A topology revision goes backward or a command is invalid.
        """
        if not self.control.poll():
            return
        command = self.control.recv()
        if command == "stop":
            self.stop()
            return
        if command == "quiesce":
            self.quiescing = True
            return
        epoch, destinations, sources = command
        if epoch <= self.link_epoch:
            raise RuntimeError("topology revision must advance")
        self.link_epoch, self.link_pending = epoch, True
        self.sources = set(sources)
        for name, peer in self.peers.items():
            if name not in destinations:
                peer.draining = True
        for name, address in destinations.items():
            if name not in self.peers:
                self.connect_peer(name, address, epoch)

    def connect_peer(self, peer: str, address: int, epoch: int) -> None:
        """
        Open a TCP link and queue a handshake for the asynchronous service loop.

        Args:
            peer (str): Root-approved destination service name.
            address (int): Destination's localhost TCP port.
            epoch (int): Root revision creating this link.

        Returns:
            None: The link is tracked but cannot receive jobs until ready arrives.
        """
        connection = socket.create_connection(("127.0.0.1", address), timeout=2)
        connection.setblocking(False)
        link = Peer(connection, peer, epoch)
        self.peers[peer] = link
        link.send("hello", source=self.name, capability="square")

    def accept_peer_work(self) -> None:
        """
        Accept a bounded number of TCP links without waiting for their messages.

        Returns:
            None: A newly accepted socket is owned until drain or cleanup.
        """
        if self.stopping or len(self.inbound) >= 3:
            return
        assert self.listener is not None
        if select([self.listener], [], [], 0)[0]:
            connection, _ = self.listener.accept()
            connection.setblocking(False)
            self.inbound.append(Peer(connection))

    def peer_capacity(self) -> int:
        """
        Advertise free work slots after accounting for local and peer demand.

        Returns:
            int: A hint bounded by the shared peer budget, rechecked on admission.
        """
        if self.stopping or self.quiescing:
            return 0
        local_work = len(self.pending) + sum(len(child.jobs) for child in self.children.values())
        return max(0, min(self.runtime.peer_window - len(self.replies), self.runtime.peer_window - local_work))

    def poll_peers(self) -> None:
        """
        Advance TCP I/O and verify each message before changing work ownership.

        Returns:
            None: Readiness, capacity, results and drain messages are processed.

        Raises:
            RuntimeError: A peer disappears with unfinished work or without draining.
        """
        for outbound, links in ((True, list(self.peers.values())), (False, list(self.inbound))):
            for peer in links:
                for message in peer.pump():
                    self.peer_message(peer, message, outbound=outbound)
                if peer.closed:
                    unfinished = peer.jobs or any(link is peer for link in self.replies.values())
                    if unfinished or peer.outgoing or not peer.draining:
                        raise RuntimeError("peer disconnected before its work and drain were acknowledged")
                    peer.connection.close()
                    if outbound:
                        self.peers.pop(peer.name)
                    else:
                        self.inbound.remove(peer)

    def peer_message(self, peer: Peer, message: dict[str, Any], *, outbound: bool) -> None:
        """
        Dispatch one message after checking its connection revision and direction.

        Args:
            peer (Peer): Owned link on which the message arrived.
            message (dict[str, Any]): Decoded protocol frame.
            outbound (bool): True when this service sends jobs over the link.

        Returns:
            None: The corresponding handshake or work handler updates the ledger.

        Raises:
            RuntimeError: A message is unauthorized, stale or invalid for this direction.
        """
        kind = message["kind"]
        if kind == "hello" and not outbound and not peer.ready:
            name, epoch = message.get("source"), message.get("epoch")
            if name not in self.sources or type(epoch) is not int or not 0 < epoch <= self.link_epoch:
                raise RuntimeError("peer handshake was not authorized by the root")
            if message.get("capability") != "square" or any(link.ready and link.name == name for link in self.inbound):
                raise RuntimeError("incompatible or duplicate peer handshake")
            peer.name, peer.epoch, peer.ready = str(name), epoch, True
            peer.send("ready", capability="square")
            return
        if type(message.get("epoch")) is not int or message.get("epoch") != peer.epoch:
            raise RuntimeError("peer message has a stale connection revision")
        if kind == "ready" and outbound and not peer.ready and message.get("capability") == "square":
            peer.ready = True
            emit("edge_opened", service=self.name, target=peer.name, epoch=peer.epoch)
        elif not peer.ready:
            raise RuntimeError("peer work arrived before readiness")
        elif kind == "capacity" and outbound:
            credit = message.get("slots")
            if type(credit) is not int or not 0 <= credit <= self.runtime.peer_window:
                raise RuntimeError("invalid peer capacity advertisement")
            peer.credit = credit
        elif kind == "jobs" and not outbound:
            self.admit_peer_batch(peer, message.get("jobs"))
        elif kind == "result" and outbound:
            self.complete_peer_job(peer, message.get("identity"), message.get("value"))
        elif kind == "rejected" and outbound:
            self.requeue_rejected(peer, message.get("identities"))
        elif kind == "drain" and not outbound:
            if any(link is peer for link in self.replies.values()):
                raise RuntimeError("sender requested drain before receiving its results")
            peer.draining = True
            peer.send("drained")
        elif kind == "drained" and outbound and peer.drain_sent and not peer.jobs:
            peer.drained = True
        else:
            raise RuntimeError(f"unexpected peer message: {kind}")

    def admit_peer_batch(self, peer: Peer, jobs: Any) -> None:
        """
        Accept an entire batch only if the receiver still has enough capacity.

        Capacity advertisements can race when two senders see the same free
        slots. This local check either accepts all jobs or explicitly rejects
        them before execution. Imported jobs run locally and are never forwarded.

        Args:
            peer (Peer): Ready sender whose identity was approved by the root.
            jobs (Any): Untrusted list of job ID/value pairs from the wire.

        Returns:
            None: Jobs enter the local queue, or a rejection returns ownership to the sender.

        Raises:
            RuntimeError: IDs, values, batch size or connection lifecycle are invalid.
        """
        if not isinstance(jobs, list) or not 1 <= len(jobs) <= self.runtime.peer_window or peer.draining:
            raise RuntimeError("invalid peer batch or draining connection")
        seen: set[int] = set()
        owner = int(peer.name[-1])
        for job in jobs:
            if not isinstance(job, list) or len(job) != 2 or any(type(value) is not int for value in job):
                raise RuntimeError("peer job must contain an integer ID and value")
            identity, value = job
            first = owner * self.runtime.jobs
            if not first <= identity < first + load_counts(self.runtime)[owner] or value != identity + 1:
                raise RuntimeError("peer job does not belong to its original producer")
            if identity in self.accepted or identity in seen:
                raise RuntimeError("duplicate peer job")
            seen.add(identity)
        if len(jobs) > self.peer_capacity():
            peer.send("rejected", identities=sorted(seen))
            peer.advertised = -1
            emit("peer_backpressure", service=self.name, source=peer.name, rejected=len(jobs))
            return
        for identity, value in jobs:
            self.accepted.add(identity)
            self.replies[identity] = peer
            self.pending.append((identity, value))
        emit("peer_batch_accepted", service=self.name, source=peer.name, jobs=sorted(seen), outstanding=len(self.replies))

    def complete_peer_job(self, peer: Peer, identity: Any, value: Any) -> None:
        """
        Finish an owned job only after the selected peer returns the correct answer.

        Args:
            peer (Peer): Destination holding the job's execution assignment.
            identity (Any): Returned job ID, checked against the retained ledger.
            value (Any): Returned square, checked against the original input.

        Returns:
            None: The job is verified exactly once and its remote slot is released.

        Raises:
            RuntimeError: The result is unknown, duplicate, from another peer or incorrect.
        """
        if type(identity) is not int or type(value) is not int or self.delegated.get(identity) is not peer:
            raise RuntimeError("unknown or duplicate peer result")
        if identity not in peer.jobs or identity in self.completed or value != (identity + 1) ** 2:
            raise RuntimeError("incorrect peer result")
        peer.jobs.remove(identity)
        del self.delegated[identity]
        self.completed.add(identity)
        self.metrics.complete(identity)
        emit("delegated_work_completed", service=self.name, target=peer.name, job=identity, result=value)

    def requeue_rejected(self, peer: Peer, identities: Any) -> None:
        """
        Return explicitly unaccepted work to the sender's local queue.

        Args:
            peer (Peer): Receiver that rejected the whole batch before execution.
            identities (Any): Job IDs returned in the rejection.

        Returns:
            None: The same jobs can be processed locally or assigned again later.

        Raises:
            RuntimeError: The rejection does not match the entire outstanding batch.
        """
        if not isinstance(identities, list) or any(type(identity) is not int for identity in identities):
            raise RuntimeError("invalid rejection IDs")
        if not identities or len(set(identities)) != len(identities) or set(identities) != peer.jobs:
            raise RuntimeError("rejection does not match outstanding work")
        for identity in reversed(identities):
            if self.delegated.pop(identity) is not peer:
                raise RuntimeError("rejection came from the wrong peer")
            self.pending.appendleft((identity, identity + 1))
        self.metrics.sent[peer.name] -= len(identities)
        peer.jobs.clear()
        peer.credit = 0

    def share_work(self) -> None:
        """
        Delegate queued producer jobs through the SDK's neighbor-routing strategy.

        Retain one local batch per worker, use only the strategy's destinations
        and recheck PeerAvailabilityStrategy against service.view before sending.
        A single batch may be outstanding per link. Receiver rejection is safe
        to retry; a lost connection with unknown execution outcome fails the run.

        Returns:
            None: Eligible surplus jobs move to tracked remote assignments.
        """
        if self.stopping or self.quiescing:
            return
        self.observe_peers(self.peer_guard.evaluate(self.view))
        if not self.sharing_available:
            return
        for name in self.routes:
            if not self.usable_peer({"node": {"name": name}}):
                continue
            peer = self.peers[name]
            own = [job for job in self.pending if job[0] in self.metrics.offered]
            count = min(peer.credit, self.runtime.peer_window, max(0, len(own) - self.active.workers * self.active.batch))
            if not count:
                continue
            jobs = own[-count:]
            for job in jobs:
                self.pending.remove(job)
                self.delegated[job[0]] = peer
                peer.jobs.add(job[0])
            peer.credit = 0
            self.metrics.sent[name] = self.metrics.sent.get(name, 0) + count
            peer.send("jobs", jobs=jobs)
            emit("work_delegated", service=self.name, target=name, jobs=[job[0] for job in jobs], epoch=peer.epoch)

    def maintain_links(self) -> None:
        """
        Advertise capacity, drain removed links and acknowledge topology readiness.

        Returns:
            None: Link removal waits for every result and the receiver's acknowledgement.
        """
        for peer in self.inbound:
            capacity = self.peer_capacity()
            if peer.ready and not peer.draining and capacity != peer.advertised:
                peer.send("capacity", slots=capacity)
                peer.advertised = capacity
        for name, peer in list(self.peers.items()):
            if peer.draining and peer.ready and not peer.jobs and not peer.drain_sent:
                peer.send("drain")
                peer.drain_sent = True
            if peer.drained and not peer.outgoing:
                peer.connection.close()
                del self.peers[name]
                emit("edge_closed", service=self.name, target=name, epoch=self.link_epoch, outstanding=0)
        if self.link_pending and all(peer.ready and not peer.draining for peer in self.peers.values()):
            self.control.send(("linked", self.name, self.link_epoch))
            self.link_pending = False

    def receive_producer_work(self) -> None:
        """
        Validate producer requests and apply backpressure before reading more work.

        IDs identify their original service, values equal ID plus one, and a
        timestamp precedes the producer's pipe write. Queued and delegated jobs
        together consume the 64-job admission budget. A root stop or end-of-input
        ends reading while previously accepted jobs still need results.

        Returns:
            None: Available valid jobs are queued within the admission limit.

        Raises:
            RuntimeError: A producer job is malformed, duplicated or out of range.
            EOFError: The producer closes without its expected end-of-input marker.
        """
        while not self.stopping and not self.ending and len(self.pending) + len(self.delegated) < 64 and self.incoming.poll():
            job = self.incoming.recv()
            if job is None:
                self.ending = True
                break
            identity, value, offered = job
            owner = int(self.name[-1])
            first = owner * self.runtime.jobs
            if (
                type(identity) is not int
                or type(value) is not int
                or identity in self.accepted
                or not first <= identity < first + load_counts(self.runtime)[owner]
                or value != identity + 1
                or type(offered) not in (int, float)
                or not math.isfinite(offered)
                or not 0 <= offered <= time.monotonic()
            ):
                raise RuntimeError("invalid or duplicate input")
            self.accepted.add(identity)
            self.metrics.offered[identity] = offered
            self.pending.append((identity, value))

    def collect_worker_results(self) -> None:
        """
        Consume worker readiness and completion receipts without admitting new work.

        Readiness marks a child as eligible for a later profile commit. Results
        go through the application's batch validator before releasing that
        worker's in-flight work record.

        Returns:
            None: Available worker messages update readiness or completed work.

        Raises:
            RuntimeError: A completed batch violates the application's contract.
            EOFError: A worker disconnects before its expected receipt.
        """
        for pid, child in self.children.items():
            if not child.pipe.poll() or child.stopping:
                continue
            kind, result = child.pipe.recv()
            if kind == "ready":
                child.ready = True
                emit("worker_ready", service=self.name, worker=pid, role=child.role)
            else:
                self.complete_batch(child, result)

    def complete_batch(self, child: Child, results: list[tuple[int, int]]) -> None:
        """
        Verify the square application's result contract and publish peer replies.

        Completion must match exactly the worker's accepted batch, including
        identity uniqueness and computed values. A worker becomes available for
        another batch only after its entire current result has been processed.

        Args:
            child (Child): Worker that owns the accepted batch.
            results (list[tuple[int, int]]): Returned identity/square pairs.

        Returns:
            None: Verified identities are recorded and the child's batch is clear.

        Raises:
            RuntimeError: Identities, values or duplicate completions violate the contract.
            OSError: A peer result cannot be delivered to its waiting connection.
        """
        if {identity for identity, _ in results} != set(child.jobs) or len(results) != len(child.jobs):
            raise RuntimeError("completion does not match its accepted batch")
        for identity, value in results:
            if value != (identity + 1) ** 2 or identity in self.completed:
                raise RuntimeError("wrong or duplicate result")
            self.completed.add(identity)
            if identity in self.replies:
                peer = self.replies.pop(identity)
                peer.send("result", identity=identity, value=value)
                self.metrics.remote_executed += 1
                emit("peer_work_completed", service=self.name, source=peer.name, job=identity, result=value)
            else:
                self.metrics.complete(identity)
        child.jobs = ()

    def reap_workers(self) -> None:
        """
        Remove exited workers only after confirming a clean drain and requested stop.

        Unexpected worker death fails the experiment rather than silently losing
        its accepted jobs. Retiring workers still count toward live capacity
        until their process has exited and been joined here.

        Returns:
            None: Cleanly exited child processes are joined and removed.

        Raises:
            RuntimeError: A child exits without a clean stop or leaves accepted work.
        """
        for pid, child in list(self.children.items()):
            if child.process.is_alive():
                continue
            child.process.join()
            if not child.stopping or child.process.exitcode != 0 or child.jobs:
                raise RuntimeError(f"worker {pid} exited before a clean drain")
            child.pipe.close()
            del self.children[pid]
            emit("worker_joined", service=self.name, worker=pid, role=child.role)

    def observe(self, now: float) -> Observation:
        """
        Measure useful progress and pressure before asking the policy what to change.

        Backlog includes queued work and every child's accepted batch, including
        retiring workers. Update sustained demand and quiet time from that view;
        emit deltas only when the measured work state changes.

        Args:
            now (float): Current monotonic observation timestamp.

        Returns:
            Observation: Immutable evidence shared by proposal and admission.
        """
        backlog = len(self.pending) + len(self.delegated) + sum(len(child.jobs) for child in self.children.values())
        self.metrics.observe(now)
        if backlog:
            self.last_busy = now
        self.high = self.high + 1 if backlog >= self.runtime.high_water else 0
        observation = Observation(
            time=now,
            backlog=backlog,
            delta_backlog=backlog - self.previous_backlog,
            completed=len(self.completed),
            completed_delta=len(self.completed) - self.previous_done,
            high=self.high,
            quiet=now - self.last_busy,
            elapsed=now - self.last_change,
        )
        if observation.delta_backlog or observation.completed_delta:
            emit(
                "observed",
                service=self.name,
                backlog=backlog,
                delta_backlog=observation.delta_backlog,
                completed=observation.completed,
                completed_delta=observation.completed_delta,
            )
        self.previous_backlog = backlog
        self.previous_done = observation.completed
        return observation

    def workers_are_steady(self) -> bool:
        """
        Check that one complete, ready generation owns dispatch without an active handoff.

        Returns:
            bool: True when workers exist, none are retiring or stopping and no
                proposed profile remains uncommitted.
        """
        return (
            bool(self.children)
            and self.candidate is None
            and all(child.ready and not child.retiring and not child.stopping for child in self.children.values())
        )

    def propose_profile(self, observation: Observation) -> None:
        """
        Ask the pure policy for a replacement only after the preceding handoff settles.

        This step changes intent, not processes. Cooldown and sustained-pressure
        decisions belong to search_soul; resource and structural admission comes
        next. Shutdown prevents further profile proposals.

        Args:
            observation (Observation): Current backlog, progress and timing evidence.

        Returns:
            None: A changed policy choice becomes the candidate profile.
        """
        if self.stopping or not self.workers_are_steady():
            return
        proposal = search_soul(
            self.active,
            observation.backlog,
            observation.high,
            observation.quiet,
            observation.elapsed,
            self.runtime,
        )
        if proposal == INTERACTIVE and not self.quiescing:
            return
        if proposal != self.active:
            self.candidate = proposal

    def admit_profile(self, observation: Observation) -> None:
        """
        Validate a candidate and start its replacements within the full overlap budget.

        The initial interactive profile follows the same admission path as later
        changes. Count old workers until they are reaped, check the hard Cheeger
        floor and demand target, and retain the old dispatch profile throughout
        replacement startup. Repeated calls do not duplicate replacements.

        Args:
            observation (Observation): Evidence attached to the admission receipt.

        Returns:
            None: An eligible candidate has replacement processes starting.

        Raises:
            RuntimeError: The candidate exceeds process, change or Cheeger limits.
        """
        candidate = self.candidate
        if candidate is None or self.stopping:
            return
        if any(child.role == candidate.role and not child.retiring for child in self.children.values()):
            return
        expansion = cheeger(candidate.workers)
        if (
            self.changes >= 4
            or len(self.children) + candidate.workers > self.runtime.worker_limit
            or expansion < max(self.runtime.hard_minimum, candidate.target)
        ):
            emit("blocked", service=self.name, role=candidate.role, live=len(self.children), worker_limit=self.runtime.worker_limit)
            raise RuntimeError("proposed profile violates the process, change or Cheeger budget")
        emit(
            "admitted",
            service=self.name,
            role=candidate.role,
            backlog=observation.backlog,
            delta_backlog=observation.delta_backlog,
            completed_delta=observation.completed_delta,
            current_cheeger=cheeger(self.active.workers),
            cheeger=expansion,
            target=candidate.target,
            hard_minimum=self.runtime.hard_minimum,
            generation=self.generation + 1,
        )
        for _ in range(candidate.workers):
            self.spawn_worker(candidate)

    def spawn_worker(self, profile: Profile) -> None:
        """
        Create and retain ownership of one already-admitted worker process.

        Admission must check the entire replacement group before this operation.
        A started worker is tracked immediately, even while it is not ready.

        Args:
            profile (Profile): Approved role and dispatch limits for the child.

        Returns:
            None: The new child and its pipe are recorded for readiness and cleanup.

        Raises:
            OSError: A pipe or process cannot be created.
        """
        parent, child_pipe = self.process_context.Pipe()
        process = self.process_context.Process(target=worker, args=(child_pipe, profile, self.runtime), name=f"{self.name}-{profile.role}")
        try:
            process.start()
        except BaseException:
            parent.close()
            raise
        finally:
            child_pipe.close()
        assert process.pid is not None
        self.children[process.pid] = Child(process, parent, profile.role)
        emit("worker_spawned", service=self.name, worker=process.pid, role=profile.role, live=len(self.children))

    def commit_profile(self, now: float) -> None:
        """
        Transfer dispatch to a candidate only when every replacement is ready.

        The old generation becomes retiring and finishes its accepted batches.
        A profile acknowledgement lets the root coordinate the outer peer graph.

        Args:
            now (float): Monotonic commit timestamp for subsequent cooldown checks.

        Returns:
            None: A ready candidate becomes active, or startup remains in progress.
        """
        candidate = self.candidate
        if candidate is None:
            return
        replacements = [child for child in self.children.values() if child.role == candidate.role and not child.retiring]
        if len(replacements) != candidate.workers or not all(child.ready for child in replacements):
            return
        for child in self.children.values():
            child.retiring = child.role != candidate.role
        self.active = candidate
        self.candidate = None
        self.generation += 1
        self.changes += self.generation > 1
        self.high = 0
        self.last_change = now
        emit(
            "committed",
            service=self.name,
            role=self.active.role,
            workers=self.active.workers,
            cheeger=cheeger(self.active.workers),
            generation=self.generation,
        )
        self.control.send(("ready" if self.generation == 1 else self.active.role, self.name, self.port))

    def dispatch_work(self) -> None:
        """
        Route bounded batches to active workers while retiring generations drain.

        A child holds at most one in-flight batch. Retiring children stop after
        that batch; root shutdown also stops active workers once the queue is
        empty. No accepted job is moved to a replacement or silently discarded.

        Returns:
            None: Eligible workers receive work or a drained stop sentinel.

        Raises:
            OSError: A worker pipe fails while a command is sent.
        """
        for child in self.children.values():
            if child.jobs or child.stopping:
                continue
            if child.retiring or (self.stopping and not self.pending):
                child.pipe.send(None)
                child.stopping = True
            elif child.ready and child.role == self.active.role and self.pending:
                batch = [self.pending.popleft() for _ in range(min(self.active.batch, len(self.pending)))]
                child.jobs = tuple(identity for identity, _ in batch)
                child.pipe.send(batch)

    def report_completion(self) -> None:
        """
        Tell the root when every producer job owned here has a verified result.

        Other services may still delegate jobs to this one. The root waits for
        all three owners before quiescing new work and waiting for worker recovery.

        Returns:
            None: Completion is reported once, independently of serving peer jobs.

        Raises:
            RuntimeError: End-of-input arrives without the configured owned job count.
        """
        if not self.ending or self.done_sent:
            return
        expected = load_counts(self.runtime)[int(self.name[-1])]
        if len(self.metrics.offered) != expected:
            raise RuntimeError("producer ended before sending the expected load")
        if len(self.metrics.latencies) == expected:
            self.done_sent = True
            self.control.send(("done", self.name, expected))

    def report_recovery(self, observation: Observation) -> None:
        """
        Verify the demonstration's full load and lifecycle before acknowledging recovery.

        After root quiesce, all owned and imported jobs must be complete and the
        service must have one ready interactive worker. Services that never
        needed a batch profile can retain their original interactive worker.

        Args:
            observation (Observation): Work ledger observed before this cycle's dispatch.

        Returns:
            None: Complete recovery is acknowledged once, when its conditions hold.

        Raises:
            RuntimeError: Recovery lacks required input or verified completion.
        """
        if (
            not self.quiescing
            or not self.ending
            or observation.backlog
            or self.returned
            or self.active != INTERACTIVE
            or not self.workers_are_steady()
        ):
            return
        if not self.done_sent or self.completed != self.accepted or self.delegated or self.replies:
            raise RuntimeError("experiment did not complete every owned and peer job")
        self.returned = True
        emit("baseline_restored", service=self.name, completed=len(self.metrics.offered), workers=len(self.children))
        self.control.send(("restored", self.name, asdict(self.metrics)))

    def finish_if_stopped(self) -> bool:
        """
        Acknowledge root shutdown only after the pending queue and owned workers are empty.

        Returns:
            bool: True when the run loop can return after sending its stop receipt.
        """
        if self.stopping and not self.pending and not self.children and not self.delegated and not self.peers and not self.inbound:
            self.control.send(("stopped", self.name, self.port))
            return True
        return False

    def close_workers(self) -> None:
        """
        Reap the owned worker subtree after a normal stop, interruption or failure.

        Ask children to stop first, use bounded joins, then terminate and finally
        kill an unresponsive child. Normal serving drains through dispatch_work;
        this fallback ensures process ownership is still honored on exceptions.

        Returns:
            None: Every remaining owned worker is joined and its pipe is closed.
        """
        for child in self.children.values():
            if child.process.is_alive() and not child.stopping:
                try:
                    child.pipe.send(None)
                except OSError:
                    pass
        for child in self.children.values():
            child.process.join(timeout=2)
            if child.process.is_alive():
                child.process.terminate()
                child.process.join(timeout=2)
            if child.process.is_alive():
                child.process.kill()
                child.process.join()
            emit("worker_cleaned", service=self.name, worker=child.process.pid, exitcode=child.process.exitcode)
            child.pipe.close()

    def close(self) -> None:
        """
        Release workers, network connections and transferred producer/control endpoints.

        The process entry point calls this from finally, including partial
        listener startup. Networking closes even if worker cleanup raises.

        Returns:
            None: Owned runtime resources are released.
        """
        try:
            self.close_workers()
        finally:
            if self.listener is not None:
                self.listener.close()
            for peer in [*self.peers.values(), *self.inbound]:
                peer.connection.close()
            self.incoming.close()
            self.control.close()


def service(name: str, incoming: Connection, control: Connection, settings: Settings) -> None:
    """
    Run one resilient application with explicit ownership and unconditional cleanup.

    This spawn entry point handles signals and the lifetime of AdaptiveService.
    Read AdaptiveService.run() for the application's lifecycle, then follow each
    named step into its admission, adaptation, readiness or draining behavior.

    Args:
        name (str): Service identity ending in its vertex index, 0 through 2.
        incoming (Connection): Read endpoint for producer jobs and end-of-input.
        control (Connection): Duplex root channel for topology, stop and status.
        settings (Settings): Workload, observation, admission and timeout limits.

    Returns:
        None: Accepted work drained, owned processes joined and sockets closed.

    Raises:
        TimeoutError: The service exceeds its experiment deadline.
        RuntimeError: A job, result, worker exit, profile or peer violates its
            contract, or the complete adaptation/recovery cycle is missing.
        OSError: Process pipes or TCP peers fail during communication.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, interrupt)
    application = AdaptiveService(name, incoming, control, settings)
    try:
        application.run()
    finally:
        application.close()


@dataclass
class ServiceProcess:
    """
    Keep one direct child and its transferred pipe endpoints under root ownership.

    Attributes:
        name (str): Stable identity used in topology and lifecycle messages.
        process (BaseProcess): Service process that owns its own worker subtree.
        control (Connection): Parent endpoint for commands and acknowledgements.
        output (Connection): Producer endpoint, transferred when load starts.
    """

    name: str
    process: BaseProcess
    control: Connection
    output: Connection


class SoulExperiment:
    """
    Walk the root through startup, pressure, topology recovery and complete shutdown.

    run() is the experiment's reading guide. Each phase waits for real service
    receipts before advancing. Services continue running their own observation
    and worker loops while the root waits for a phase to finish.

    Attributes:
        settings (Settings): Validated timing, load and structural limits.
        context (mp.context.SpawnContext): Context for direct child processes.
        services (list[ServiceProcess]): Owned service processes and endpoints.
        generator (BaseProcess | None): Owned producer after load starts.
        ports (dict[str, int]): Listener ports announced by ready services.
        receipts (dict[str, set[str]]): Service acknowledgements grouped by kind.
        epoch (int): Current admitted topology revision.
        topology (str): Current graph layout, initially starting.
        deadline (float): Absolute monotonic deadline for the whole experiment.
        adaptive (bool): Whether sustained S0 demand may add the direct S0-to-S2 link.
        measurements (dict[str, dict[str, Any]]): Verified service measurements at recovery.
        started (float): Time load generation was started, excluding service startup.
    """

    settings: Settings
    context: mp.context.SpawnContext
    services: list[ServiceProcess]
    generator: BaseProcess | None
    ports: dict[str, int]
    receipts: dict[str, set[str]]
    epoch: int
    topology: str
    deadline: float
    adaptive: bool
    measurements: dict[str, dict[str, Any]]
    started: float

    def __init__(self, settings: Settings, *, adaptive: bool = True) -> None:
        """
        Initialize root ownership and phase evidence without creating processes.

        Args:
            settings (Settings): Validated experiment configuration.
            adaptive (bool): Enable the demand-triggered shortcut, or retain the chain.
        """
        self.settings = settings
        self.context = mp.get_context("spawn")
        self.services = []
        self.generator = None
        self.ports = {}
        self.receipts = {kind: set() for kind in ("ready", "batch", "linked", "done", "restored", "stopped")}
        self.epoch = 0
        self.topology = "starting"
        self.deadline = time.monotonic() + settings.timeout
        self.adaptive = adaptive
        self.measurements = {}
        self.started = 0

    def run(self) -> dict[str, Any]:
        """
        Execute the chain, pressure, triangle, recovery and shutdown phases in order.

        Worker adaptation happens independently inside each service. The root
        changes peer connectivity only after the corresponding phase evidence
        arrives, and always checks the structural floor before a graph change.

        Returns:
            dict[str, Any]: Verified performance measurements after every child has exited.

        Raises:
            RuntimeError: Admission, a child, a phase deadline or final verification fails.
        """
        emit("trial_started", mode="adaptive" if self.adaptive else "chain", counts=load_counts(self.settings))
        self.start_services()
        self.wait_for("ready")
        self.connect_services("chain")
        self.start_load()

        if self.adaptive:
            self.wait_for("batch", count=1, service_name="service-0")
            self.connect_services("triangle")
        self.wait_for("done")
        for member in self.services:
            member.control.send("quiesce")
        self.wait_for("restored")
        if self.adaptive:
            self.connect_services("chain")

        self.stop_services()
        self.verify_shutdown()
        result = self.results()
        emit("trial_verified", **result)
        return result

    def results(self) -> dict[str, Any]:
        """
        Summarize measured work instead of treating Cheeger as a throughput result.

        Completion time starts when the root launches the producer and ends at
        the final owned result. Latencies include producer pipe backpressure;
        backlog area covers work admitted by S0, including delegated jobs.

        Returns:
            dict[str, Any]: Counts, time, latency, source backlog and peer-use evidence.

        Raises:
            RuntimeError: Counts disagree or the adaptive shortcut carried no owned work.
        """
        completed = sum(len(value["latencies"]) for value in self.measurements.values())
        shortcut = self.measurements["service-0"]["sent"].get("service-2", 0)
        if completed != sum(load_counts(self.settings)) or (self.adaptive and not shortcut):
            raise RuntimeError("trial did not verify its load and useful shortcut work")
        latencies = sorted(latency for value in self.measurements.values() for latency in value["latencies"])
        elapsed = max(value["finished"] for value in self.measurements.values()) - self.started
        return {
            "mode": "adaptive" if self.adaptive else "chain",
            "completed": completed,
            "elapsed_seconds": round(elapsed, 6),
            "jobs_per_second": round(completed / elapsed, 3),
            "mean_latency_seconds": round(sum(latencies) / completed, 6),
            "p95_latency_seconds": round(latencies[math.ceil(completed * 0.95) - 1], 6),
            "source_backlog_seconds": round(self.measurements["service-0"]["backlog_seconds"], 6),
            "source_peak_backlog": self.measurements["service-0"]["peak_backlog"],
            "shortcut_jobs": shortcut,
            "peer_jobs": sum(value["remote_executed"] for value in self.measurements.values()),
            "worker_limit_per_service": self.settings.worker_limit,
            "worker_ceiling": 3 * self.settings.worker_limit,
            "peer_window": self.settings.peer_window,
            "all_joined": True,
        }

    def start_services(self) -> None:
        """
        Start the three application processes with independently owned worker subtrees.

        Returns:
            None: All three service processes are tracked while they become ready.

        Raises:
            OSError: A service process or its communication pipes cannot be created.
        """
        for index in range(3):
            self.start_service(f"service-{index}")

    def start_service(self, name: str) -> None:
        """
        Spawn one service and retain its root-side endpoints for control and cleanup.

        Child-only endpoints close in the parent immediately after startup. If
        process creation fails, both root-side endpoints are closed as well.

        Args:
            name (str): Service identity ending in its vertex index.

        Returns:
            None: The started process and root endpoints are registered together.

        Raises:
            OSError: Pipe allocation or service process startup fails.
        """
        incoming, output = self.context.Pipe(duplex=False)
        parent, child = self.context.Pipe()
        process = self.context.Process(target=service, args=(name, incoming, child, self.settings), name=name)
        try:
            process.start()
        except BaseException:
            parent.close()
            output.close()
            raise
        finally:
            incoming.close()
            child.close()
        self.services.append(ServiceProcess(name, process, parent, output))

    def receive_reports(self) -> None:
        """
        Record readiness, profile and lifecycle evidence from live service channels.

        A linked acknowledgement counts only for the current topology epoch.
        Other receipts remain available across phases, so recovery reported
        during a topology handoff is preserved for the subsequent wait.

        Returns:
            None: Available receipts and service listener ports are recorded.

        Raises:
            EOFError: A service closes before acknowledging its shutdown.
        """
        for member in self.services:
            if member.name in self.receipts["stopped"] or not member.control.poll():
                continue
            kind, name, value = member.control.recv()
            if kind == "ready":
                self.ports[name] = value
            if kind == "restored":
                self.measurements[name] = value
            if kind == "linked" and value != self.epoch:
                continue
            if kind in self.receipts:
                self.receipts[kind].add(name)

    def check_health(self) -> None:
        """
        Reject an expired experiment or any failed direct child before advancing a phase.

        Returns:
            None: The experiment is within its deadline and no child has failed.

        Raises:
            RuntimeError: The deadline expires or a direct child exits unsuccessfully.
        """
        failed = any(process.exitcode not in (None, 0) for process in self.owned_processes())
        if time.monotonic() > self.deadline or failed:
            raise RuntimeError("experiment deadline or child failure; stopping the tree")

    def wait_for(self, kind: str, count: int = 3, *, service_name: str | None = None) -> None:
        """
        Wait for a named phase while continuing to collect all service observations.

        Args:
            kind (str): Receipt kind, such as ready, batch, linked or restored.
            count (int): Number of distinct services required to acknowledge it.
            service_name (str | None): Also require a particular service's acknowledgement.

        Returns:
            None: Enough services acknowledged the requested phase.

        Raises:
            RuntimeError: Health checks detect a failed child or expired deadline.
            EOFError: A service disconnects before its clean shutdown receipt.
        """
        while len(self.receipts[kind]) < count or (service_name is not None and service_name not in self.receipts[kind]):
            self.check_health()
            self.receive_reports()
            time.sleep(self.settings.tick)

    def connect_services(self, topology: str) -> None:
        """
        Admit a peer layout, send its revision and wait for verified TCP connections.

        Services complete a capability handshake before acknowledging new links.
        Removed links finish all results and exchange a drain acknowledgement.
        Useful application work is measured separately after the load completes.

        Args:
            topology (str): Approved layout name, either chain or triangle.

        Returns:
            None: All three services acknowledged the admitted topology revision.

        Raises:
            RuntimeError: The graph fails its structural bounds or a phase fails.
            ValueError: The requested layout is not an approved demonstration shape.
        """
        if topology not in {"chain", "triangle"}:
            raise ValueError("choose the chain or triangle layout")
        edges = [(0, 1), (1, 2)]
        if topology == "triangle":
            edges.append((0, 2))
        target = 2 if topology == "triangle" else 1
        expansion = cheeger(links=edges)
        if expansion < max(self.settings.hard_minimum, target):
            raise RuntimeError("peer topology violates its Cheeger constraints")

        self.topology = topology
        self.epoch += 1
        self.receipts["linked"].clear()
        emit("topology_admitted", topology=topology, epoch=self.epoch, edges=edges, cheeger=expansion, target=target)
        for index, member in enumerate(self.services):
            destinations = {f"service-{b}": self.ports[f"service-{b}"] for a, b in edges if a == index}
            sources = [f"service-{a}" for a, b in edges if b == index]
            member.control.send((self.epoch, destinations, sources))
        self.wait_for("linked")
        emit("topology_committed", topology=topology, epoch=self.epoch)

    def start_load(self) -> None:
        """
        Transfer the producer pipes to a finite load generator after the chain is ready.

        Returns:
            None: The owned producer has started sending warmup and burst traffic.

        Raises:
            OSError: The producer cannot be started.
        """
        outputs = [member.output for member in self.services]
        generator = self.context.Process(target=skewed_producer, args=(outputs, self.settings), name="load-generator")
        self.started = time.monotonic()
        generator.start()
        self.generator = generator
        emit("load_started", producer=generator.pid, services=[member.process.pid for member in self.services])
        for output in outputs:
            output.close()

    def stop_services(self) -> None:
        """
        Request graceful subtree shutdown after work and the original topology recover.

        Returns:
            None: Every service has acknowledged that its workers finished and exited.

        Raises:
            RuntimeError: A service fails to stop before the experiment deadline.
        """
        for member in self.services:
            member.control.send("stop")
        self.wait_for("stopped")

    def owned_processes(self) -> list[BaseProcess]:
        """
        Enumerate direct children for health checks, joining and failure cleanup.

        Returns:
            list[BaseProcess]: Service processes followed by the producer if started.
        """
        processes = [member.process for member in self.services]
        if self.generator is not None:
            processes.append(self.generator)
        return processes

    def verify_shutdown(self) -> None:
        """
        Join every direct child and verify its exit before reporting success.

        Returns:
            None: All owned service and producer processes have exited successfully.

        Raises:
            RuntimeError: A child fails to exit cleanly within the join deadline.
        """
        for process in self.owned_processes():
            process.join(timeout=3)
            if process.exitcode != 0:
                raise RuntimeError(f"{process.name} did not exit cleanly")

    def close(self) -> None:
        """
        Retain ownership on partial startup, cancellation or a failed experiment phase.

        Request service stops first, then join, terminate and finally kill an
        unresponsive direct child. Services clean up their own worker subtrees.

        Returns:
            None: Direct children are joined and root-side pipe endpoints are closed.
        """
        for member in self.services:
            try:
                member.control.send("stop")
            except OSError:
                pass
        for process in self.owned_processes():
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)
            if process.is_alive():
                process.kill()
                process.join()
        for member in self.services:
            member.control.close()
            member.output.close()


def parse_settings() -> tuple[Settings, str]:
    """
    Parse and validate the experiment's command-line controls before creating children.

    Returns:
        tuple[Settings, str]: Checked limits and compare, chain or adaptive mode.

    Raises:
        SystemExit: Argparse handles help or rejects unsupported limits.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("compare", "chain", "adaptive"), default="compare")
    for item in fields(Settings):
        parser.add_argument("--" + item.name.replace("_", "-"), type=type(item.default), default=item.default)
    options = vars(parser.parse_args())
    mode = options.pop("mode")
    settings = Settings(**options)
    if any(not math.isfinite(value) or value <= 0 for value in vars(settings).values()) or not 96 <= settings.jobs <= 2000:
        parser.error("use finite positive limits and 96 through 2000 jobs for S0")
    if settings.worker_limit > 6 or settings.hard_minimum > cheeger(1):
        parser.error("worker-limit must be at most six; the initial graph must satisfy the hard minimum")
    if settings.peer_window > 64:
        parser.error("peer-window must be at most 64 jobs")
    return settings, mode


def main() -> None:
    """
    Configure the root, run its named experiment phases and always release ownership.

    Start here, then read SoulExperiment.run() for the experiment's phase order
    and AdaptiveService.run() for the concurrent application lifecycle.

    Returns:
        None: The verified experiment finished and all owned processes were joined.

    Raises:
        SystemExit: Command-line help or validation ends the invocation.
        RuntimeError: A topology, child, deadline or shutdown check fails.
        KeyboardInterrupt: Cancellation unwinds through experiment cleanup.
    """
    settings, mode = parse_settings()
    signal.signal(signal.SIGTERM, interrupt)
    results = []
    for adaptive in (False, True) if mode == "compare" else (mode == "adaptive",):
        experiment = SoulExperiment(settings, adaptive=adaptive)
        try:
            results.append(experiment.run())
        finally:
            experiment.close()
    if mode == "compare":
        baseline, adapted = results
        emit(
            "comparison",
            chain=baseline,
            adaptive=adapted,
            speedup=round(baseline["elapsed_seconds"] / adapted["elapsed_seconds"], 3),
            source_backlog_reduction_seconds=round(baseline["source_backlog_seconds"] - adapted["source_backlog_seconds"], 6),
            same_load=True,
            same_worker_limits=True,
        )
    emit("success", trials=len(results), completed=sum(result["completed"] for result in results), all_joined=True)


if __name__ == "__main__":
    main()
