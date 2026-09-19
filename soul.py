"""
Run Soul Search locally with real processes, TCP peers and adaptive workers.

Run it from the repository root with the local SDK installed:

    python -m pip install ./pkg/polyad-types ./pkg/polyad-sdk
    python soul.py
    python soul.py --jobs 128 --work-seconds 0.02 --tick 0.02
    python soul.py --help

Reading order
-------------
Start with main() at the bottom, then follow the two supervisors:

    main -> parse_settings -> SoulExperiment.run -> close in finally
                                  |
                                  +-- start services and form a chain
                                  +-- start load and wait for batch pressure
                                  +-- form a triangle and wait for recovery
                                  +-- restore the chain, stop and verify

    service -> AdaptiveService.run -> receive / observe / publish / dispatch
                   +-- close in finally

AdaptiveService extends polyad_sdk.AdaptiveService. Local observations pass
through the SDK's refresh() and dispatch() methods, which run the configured
FreshnessStrategy before delivering immutable Change objects to adapt().
A local snapshot adapter supplies the worker graph;
the example needs no operator or HTTP server.

Each run method names the steps in execution order. Read its helpers for the
input contracts, transport and readiness details; the examples below explain
why those steps change the graphs. Full Google-style docstrings describe each
step's inputs, results and failure conditions.

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
pressure. AdaptiveService checks constraints and enacts worker changes;
SoulExperiment does the same for the peer network. The defaults require three observations of at
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

How to read the application lifecycle
------------------------------------
service() owns signals and a try/finally cleanup boundary. Start with
AdaptiveService.run() to see the application cycle, then follow its named steps:

    root commands / peer work / producer work
                        |
                        v
    bounded admission -> collect results -> observe pressure and progress
                                                |
                                                v
                                  SDK refresh / dispatch -> adapt(Change)
                                                |
                                                v
                  propose profile -> admit budgets -> commit when ready
                                                            |
                                                            v
                  verify recovery <- dispatch work / drain old workers

The application-specific boundaries are worker() for computation,
receive_producer_work() and accept_peer_work() for input contracts, and
complete_batch() for output verification. Keep those contracts consistent when
changing the capability. search_soul() remains a pure policy; propose_profile()
does not start processes. admit_profile() checks overlapping generations before
spawning, and commit_profile() waits for the whole replacement set to be ready.

Backpressure prevents unbounded admission. Readiness prevents premature routing.
Identity tracking exposes lost or duplicate work. Retiring workers drain instead
of abandoning accepted batches. close() runs even after partial startup or an
exception. report_recovery() separately checks this finite demonstration's
expected load and adaptation; replace that assertion for a long-lived service.

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
Run nature.py to see that local parent planner reuse this worker and policy code,
select capabilities, preserve useful services and retire excluded processes.
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
from dataclasses import asdict, dataclass, fields
from select import select
from typing import TYPE_CHECKING

from polyad_sdk import AdaptiveService as SDKAdaptiveService
from polyad_sdk import Client, FreshnessStrategy
from polyad_types import ServiceEndpoint
from polyad_types.events.envelope import Event

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess
    from types import FrameType
    from typing import Any, NoReturn

    from polyad_sdk import Change, ConstraintAssessment


@dataclass(frozen=True)
class Settings:
    """
    Set finite load, stabilization, process and runtime budgets.

    The command-line entry point validates these values before starting any
    processes. Timings are monotonic seconds; structural bounds are unweighted
    edge expansion. Worker limits include retiring and replacement generations.

    Attributes:
        jobs (int): Number of producer jobs delivered to each service.
        work_seconds (float): Simulated I/O overhead for each worker dispatch.
        tick (float): Delay between service and root observation cycles.
        high_water (int): Backlog that counts as a high-demand observation.
        sustained (int): Consecutive high-demand observations required to adapt.
        cooldown (float): Minimum elapsed time between worker-profile changes.
        idle_seconds (float): Quiet interval required to restore the baseline.
        worker_limit (int): Maximum live child workers owned by one service.
        hard_minimum (float): Fixed minimum Cheeger value for admitted graphs.
        timeout (float): Maximum duration of a service or root experiment loop.
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
    import json

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
    Soul passes three service pipes, while Nature passes its one routing pipe.
    Blocking OS pipes provide backpressure to the finite producer.

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
        backlog (int): Accepted jobs waiting or assigned to workers.
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
        peers (dict[str, socket.socket]): Outbound, root-admitted TCP connections.
        inbound (list[socket.socket]): Accepted incoming peer connections.
        replies (dict[int, socket.socket]): Peer jobs awaiting result delivery.
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
    peers: dict[str, socket.socket]
    inbound: list[socket.socket]
    replies: dict[int, socket.socket]
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
        super().__init__(
            ServiceEndpoint("", "local", "Graph", name, f"local-{os.getpid()}-{name}", "dispatch"),
            LocalObservations(self.neighborhood),
            strategies=(FreshnessStrategy("profile-admission", self.observe_freshness),),
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
            self.retire_closed_peers()
            self.collect_worker_results()
            self.reap_workers()
            self.receive_producer_work()

            observation = self.observe(time.monotonic())
            self.publish_observation(observation)
            self.admit_profile(observation)
            self.commit_profile(observation.time)
            self.dispatch_work()
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
            "revision": str(self.generation),
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
            ],
        }

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
        Apply one root topology decision or revoke admissions for shutdown.

        The root owns peer layout; this service owns enacting and acknowledging
        it. A link is acknowledged only after an actual peer job succeeds.

        Returns:
            None: An available command has updated connection or shutdown state.

        Raises:
            RuntimeError: A newly connected peer returns an incorrect result.
            OSError: The control channel or a peer connection fails.
        """
        if not self.control.poll():
            return
        command = self.control.recv()
        if command == "stop":
            self.stop()
            return
        epoch, destinations = command
        for peer in list(self.peers):
            if peer not in destinations:
                self.peers.pop(peer).close()
                emit("edge_closed", service=self.name, target=peer, epoch=epoch)
        for peer, address in destinations.items():
            if peer not in self.peers:
                self.connect_peer(peer, address, epoch)
        self.control.send(("linked", self.name, epoch))

    def connect_peer(self, peer: str, address: int, epoch: int) -> None:
        """
        Open a persistent connection and verify useful work before reporting it.

        Negative job values keep peer work distinct from producer identities.
        The finite exchange has a socket timeout; the connection remains open
        after its receipt until a later topology revision or final cleanup.

        Args:
            peer (str): Destination service name ending in its vertex index.
            address (int): Localhost TCP port announced by that destination.
            epoch (int): Root topology revision admitting this connection.

        Returns:
            None: The owned peer connection has produced a verified square result.

        Raises:
            RuntimeError: The peer returns an incorrect result.
            OSError: Connection establishment or the work exchange fails.
        """
        connection = socket.create_connection(("127.0.0.1", address), timeout=5)
        self.peers[peer] = connection
        value = -(1 + int(self.name[-1]) * 3 + int(peer[-1]) + epoch * 10)
        connection.sendall(f"{value}\n".encode())
        with connection.makefile("rb") as stream:
            result = int(stream.readline(128))
        if result != value * value:
            raise RuntimeError("peer returned an incorrect work result")
        emit("edge_opened", service=self.name, target=peer, epoch=epoch, result=result)

    def accept_peer_work(self) -> None:
        """
        Admit one peer request only when the connection and pending-work budgets allow it.

        This is the TCP input adapter for the square application. Keep accepted
        connections owned until the reply completes or cleanup runs. Producer
        and peer requests share the same bounded queue and completion ledger.

        Returns:
            None: An available valid request is queued, or admission is deferred.

        Raises:
            RuntimeError: A peer identity is invalid or was already admitted.
            OSError: Accepting or reading a peer request fails.
        """
        if self.stopping or len(self.pending) >= 64 or len(self.inbound) >= 3:
            return
        assert self.listener is not None
        if not select([self.listener], [], [], 0)[0]:
            return
        connection, _ = self.listener.accept()
        self.inbound.append(connection)
        connection.settimeout(5)
        with connection.makefile("rb") as stream:
            value = int(stream.readline(128))
        identity = value - 1
        if identity >= 0 or identity in self.accepted:
            raise RuntimeError("invalid peer work identity")
        self.accepted.add(identity)
        self.replies[identity] = connection
        self.pending.append((identity, value))

    def retire_closed_peers(self) -> None:
        """
        Release closed inbound links after their accepted work has been answered.

        A peer connection carries one work exchange in this demonstration.
        Connections still awaiting a worker result remain outside this sweep.

        Returns:
            None: Closed idle connections are removed from the owned inbound set.

        Raises:
            RuntimeError: An idle peer sends unexpected additional work.
            OSError: Inspecting the peer connection fails.
        """
        idle_links = [connection for connection in self.inbound if connection not in self.replies.values()]
        for connection in select(idle_links, [], [], 0)[0]:
            if connection.recv(1):
                raise RuntimeError("unexpected data after the peer's work receipt")
            self.inbound.remove(connection)
            connection.close()

    def receive_producer_work(self) -> None:
        """
        Validate producer requests and apply backpressure before reading more work.

        The producer contract pairs each identity with value identity + 1.
        Do not read beyond the 64-job pending budget. A root stop or producer
        end-of-input closes admission while previously accepted work remains.

        Returns:
            None: Available valid jobs are queued within the admission limit.

        Raises:
            RuntimeError: A producer job is malformed, duplicated or out of range.
            EOFError: The producer closes without its expected end-of-input marker.
        """
        while not self.stopping and not self.ending and len(self.pending) < 64 and self.incoming.poll():
            job = self.incoming.recv()
            if job is None:
                self.ending = True
                break
            identity, value = job
            if identity in self.accepted or identity not in range(self.runtime.jobs) or value != identity + 1:
                raise RuntimeError("invalid or duplicate input")
            self.accepted.add(identity)
            self.pending.append(job)

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
                self.replies.pop(identity).sendall(f"{value}\n".encode())
                emit("peer_work_completed", service=self.name, job=identity, result=value)
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
        backlog = len(self.pending) + sum(len(child.jobs) for child in self.children.values())
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

    def report_recovery(self, observation: Observation) -> None:
        """
        Verify the demonstration's full load and lifecycle before acknowledging recovery.

        This experiment-specific assertion is separate from the application's
        serving loop: all configured producer work and every accepted peer job
        must complete, with adaptation followed by a stable interactive profile.

        Args:
            observation (Observation): Work ledger observed before this cycle's dispatch.

        Returns:
            None: Complete recovery is acknowledged once, when its conditions hold.

        Raises:
            RuntimeError: Recovery lacks adaptation, required input or verified completion.
        """
        if not self.ending or observation.backlog or self.returned or self.active != INTERACTIVE or not self.workers_are_steady():
            return
        producer_jobs = self.accepted.intersection(range(self.runtime.jobs))
        if self.generation < 3 or len(producer_jobs) != self.runtime.jobs or self.completed != self.accepted:
            raise RuntimeError("experiment did not adapt, restore and complete every job")
        self.returned = True
        emit("baseline_restored", service=self.name, completed=self.runtime.jobs, workers=len(self.children))
        self.control.send(("restored", self.name, self.port))

    def finish_if_stopped(self) -> bool:
        """
        Acknowledge root shutdown only after the pending queue and owned workers are empty.

        Returns:
            bool: True when the run loop can return after sending its stop receipt.
        """
        if self.stopping and not self.pending and not self.children:
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
            for connection in [*self.peers.values(), *self.inbound]:
                connection.close()
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

    def __init__(self, settings: Settings) -> None:
        """
        Initialize root ownership and phase evidence without creating processes.

        Args:
            settings (Settings): Validated experiment configuration.
        """
        self.settings = settings
        self.context = mp.get_context("spawn")
        self.services = []
        self.generator = None
        self.ports = {}
        self.receipts = {kind: set() for kind in ("ready", "batch", "linked", "restored", "stopped")}
        self.epoch = 0
        self.topology = "starting"
        self.deadline = time.monotonic() + settings.timeout

    def run(self) -> None:
        """
        Execute the chain, pressure, triangle, recovery and shutdown phases in order.

        Worker adaptation happens independently inside each service. The root
        changes peer connectivity only after the corresponding phase evidence
        arrives, and always checks the structural floor before a graph change.

        Returns:
            None: The original chain is restored and every direct child is joined.

        Raises:
            RuntimeError: Admission, a child, a phase deadline or final verification fails.
        """
        self.start_services()
        self.wait_for("ready")
        self.connect_services("chain")
        self.start_load()

        self.wait_for("batch", count=2)
        self.connect_services("triangle")
        self.wait_for("restored")
        self.connect_services("chain")

        self.stop_services()
        self.verify_shutdown()
        emit("success", services=3, completed=3 * self.settings.jobs, topology=self.topology, all_joined=True)

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

    def wait_for(self, kind: str, count: int = 3) -> None:
        """
        Wait for a named phase while continuing to collect all service observations.

        Args:
            kind (str): Receipt kind, such as ready, batch, linked or restored.
            count (int): Number of distinct services required to acknowledge it.

        Returns:
            None: Enough services acknowledged the requested phase.

        Raises:
            RuntimeError: Health checks detect a failed child or expired deadline.
            EOFError: A service disconnects before its clean shutdown receipt.
        """
        while len(self.receipts[kind]) < count:
            self.check_health()
            self.receive_reports()
            if len(self.receipts[kind]) < count:
                time.sleep(self.settings.tick)

    def connect_services(self, topology: str) -> None:
        """
        Admit a peer layout, send its revision and wait for verified TCP connections.

        Each service executes its edge changes and verifies a job through each
        new connection before acknowledging the revision. Publishing the commit
        therefore follows actual network readiness, not command delivery alone.

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
            member.control.send((self.epoch, destinations))
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
        generator = self.context.Process(target=producer, args=(outputs, self.settings), name="load-generator")
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


def parse_settings() -> Settings:
    """
    Parse and validate the experiment's command-line controls before creating children.

    Returns:
        Settings: Finite, positive configuration with a feasible initial graph.

    Raises:
        SystemExit: Argparse handles help or rejects unsupported limits.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for item in fields(Settings):
        parser.add_argument("--" + item.name.replace("_", "-"), type=type(item.default), default=item.default)
    settings = Settings(**vars(parser.parse_args()))
    if any(not math.isfinite(value) or value <= 0 for value in vars(settings).values()) or not 24 <= settings.jobs <= 2000:
        parser.error("use finite positive limits and 24 through 2000 jobs per service")
    if settings.worker_limit > 6 or settings.hard_minimum > cheeger(1):
        parser.error("worker-limit must be at most six; the initial graph must satisfy the hard minimum")
    return settings


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
    settings = parse_settings()
    signal.signal(signal.SIGTERM, interrupt)
    experiment = SoulExperiment(settings)
    try:
        experiment.run()
    finally:
        experiment.close()


if __name__ == "__main__":
    main()
