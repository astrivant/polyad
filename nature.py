"""
Run Natural Selection above Soul searching in a local tree of real processes.

Keep this script beside soul.py and install its local SDK dependency:

    python -m pip install ./pkg/polyad-types ./pkg/polyad-sdk
    python nature.py
    python nature.py --jobs 128
    python nature.py --help

Reading order
-------------
Start with main() at the bottom, then read the short orchestration methods:

    main -> parse_settings -> Nature.run -> close in finally
                                  |
                                  +-- for each environment:
                                  |       apply -> load and verify
                                  +-- retire the final population

    apply -> select -> prepare -> readiness -> revision -> commit -> retire
    load  -> start -> [admit jobs -> route results -> check health] -> verify
    serve -> Service.run -> receive / observe / adapt / dispatch / recover

LoadRound holds per-environment routing state. Service.run has the same
observe, propose, admit and commit steps as Soul's AdaptiveService.run. Follow
the named helpers when you need transport or lifecycle details; their extended
Google-style docstrings define arguments, return values and failure conditions.

Who owns what
-------------
Natural Selection chooses WHICH services and capabilities make up the graph.
Soul searching chooses HOW each selected service uses its child workers.

    nature.py (parent: requirements, placement, routing, plan revisions)
    +-- producer (a fresh finite load for each environment)
    +-- service A (Soul searching)
    |   +-- interactive worker OR three batch workers
    +-- service B (Soul searching)
    |   +-- interactive worker OR three batch workers
    +-- service C, later replaced by D (Soul searching)
        +-- interactive worker OR three batch workers

This imports soul.py's actual worker, producer, profile-selection function,
worker-routing Cheeger calculation and lifecycle logging. The parent provides
the changing capability contracts and composition around that machinery.

Environment 1: all three can survive
-----------------------------------
The required result is x*x. Three independent routes are required, with a total
capability cost of at most 4. A and B cost 1 each; C costs 2. All three fit:

                    +--> A: square --+
    input ----------+--> B: square --+----------> verified output
                    +--> C: square --+

    Composition Cheeger = 1.5. Three service processes are alive.

The producer creates pressure. Every service uses Soul searching to roll from
one interactive worker to three batch workers, drain, then return to one:

    service PID stays alive
    +-- interactive       +-- batch-1       +-- interactive (new worker PID)
                          +-- batch-2
                          +-- batch-3

Environment 2: the requirements change
--------------------------------------
Now the result must be x*x + 1, with two independent routes and a cost ceiling
of 3. The catalog gives A an approved fused square-plus-one capability. D can
increment an already-squared value. B and C can only square values.

                    +--> A': square-plus-one --------+
    input ----------+                               +--> verified output
                    +--> B: square --> D: increment -+

    Composition Cheeger = 1.0. A', B and D are alive; old A and C have exited.

The planner enumerates compatible paths and disjoint path combinations. It
checks types, cost, service counts, rolling overlap and the hard Cheeger floor.
It ranks feasible plans by cost, then by how few incarnations must change.

    A: can mutate -> a ready replacement A' gets a new PID and capability.
    B: stays useful -> its service PID survives and feeds the new D stage.
    C: stays fixed -> B + D is cheaper than C + D; C loses its place and dies.
    D: fills a missing capability -> a new service process is born.

"Complacent" describes C's fixed capability catalog. Remaining unchanged does
not itself cause retirement: B survives unchanged because its work still fits
the outcome and budget. Death means draining and joining the retired process.
Mutation selects executable implementations from a finite, approved catalog.

Environment 3: stability is useful too
-------------------------------------
The square-plus-one requirement persists. The same A', B and D service PIDs
survive another load round. Their worker PIDs still change as Soul searching
responds to pressure and recovery. No composition mutation is needed.

    A', B, D (same service PIDs) -> finish all work -> graceful shutdown
    Final population: empty. Every producer, service and worker is joined.

What the arrows mean
-------------------
The composition drawings describe actual job routes. The parent relays each
edge through bounded multiprocessing pipes: it sends B's square result to D,
and checks D's final result. Services do not open direct TCP connections in this
example. soul.py separately demonstrates a changing TCP peer network.

    producer --> Nature --> B --result--> Nature --> D --> Nature verifies
                               squared               squared-plus-one

Each service's internal graph is D(dispatch) -> workers -> C(collect), with
Cheeger 1 for one worker and 1.5 for three. These values are independent of the
outer composition's Cheeger. The fixed floor applies at both boundaries.
Measured completion counts, backlog deltas and rates accompany the structural
measurements; correctness and completion before the deadline verify each round.

Admission and retirement
------------------------
Between environments, the parent drains the old plan, starts replacements and
waits for readiness, fences admissions with a new revision, commits routing,
then stops excluded incarnations. The live-service ceiling includes overlap:

    [A, B, C] + [A', D] -> [A', B, D] + [old A, C drained and joined]
           5 live                        3 live

Soul searching can roll workers within the active capability, but cannot change
its capability, revive C or restore old routes. Natural Selection owns those
decisions. Insufficient budgets or a failed child block the experiment and enter
cleanup. A run reports success only after verified work and complete shutdown.

Read the JSON lines as a story
-----------------------------
    selection             requirement, selected routes, cost and Cheeger
    born / mutated        new service PID, capability and predecessor
    survived              an existing service PID remains in the plan
    plan_committed        revision, routes and added/removed logical edges
    retired               reason, old PID and clean exit after draining
    committed             Soul searching worker role and generation
    observed              per-service backlog and completion deltas
    round_verified        exact results, throughput and surviving service PIDs
    nature_success        all three environments finished; no live children

The default run verifies 672 producer jobs. Multi-stage routes perform extra
intermediate work. Settings below expose load, timing, search and process limits.
See docs/workloads/local-natural-selection.md for the walkthrough.
"""

from __future__ import annotations

import argparse
import itertools
import math
import multiprocessing as mp
import signal
import time
from collections import deque
from dataclasses import dataclass, field, fields, replace
from typing import TYPE_CHECKING

import soul

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess
    from typing import Any


def square(value: int) -> int:
    """
    Transform a raw integer into its square.

    Args:
        value (int): Raw producer value.

    Returns:
        int: The squared value, satisfying the squared output contract.
    """
    return value * value


def increment(value: int) -> int:
    """
    Enrich an already-squared integer.

    Args:
        value (int): Result delivered by an upstream square capability.

    Returns:
        int: The upstream result plus one, satisfying the enriched contract.
    """
    return value + 1


def fused(value: int) -> int:
    """
    Produce the enriched result in one capability.

    This implementation satisfies the same output contract as the composed
    square-then-increment pipeline, allowing the planner to compare both forms.

    Args:
        value (int): Raw producer value.

    Returns:
        int: The square of the input plus one.
    """
    return value * value + 1


@dataclass(frozen=True)
class Capability:
    """
    Declare an executable implementation and its semantic input/output contract.

    The semantic types distinguish raw, squared and enriched integers even
    though their Python representation is the same. The parent checks these
    types while composing paths; workers execute the selected function.

    Attributes:
        name (str): Stable name for the approved implementation.
        input (str): Semantic type required at the capability's input.
        output (str): Semantic type produced by the capability.
        compute (Callable[[int], int]): Pure, spawn-importable implementation.
    """

    name: str
    input: str
    output: str
    compute: Callable[[int], int]


SQUARE = Capability("square", "raw", "squared", square)
INCREMENT = Capability("increment", "squared", "enriched", increment)
FUSED = Capability("square-plus-one", "raw", "enriched", fused)


@dataclass(frozen=True)
class Placement:
    """
    Assign one approved capability and its resource cost to a logical service.

    Multiple catalog entries may give a service alternative capabilities. A
    selected plan admits exactly one of those entries for each service name.

    Attributes:
        name (str): Logical service identity preserved across mutations.
        capability (Capability): Implementation that this incarnation executes.
        cost (int): Declared steady-state resource cost used for plan ranking.
    """

    name: str
    capability: Capability
    cost: int = 1


CATALOG = (Placement("A", SQUARE), Placement("A", FUSED), Placement("B", SQUARE), Placement("C", SQUARE, 2), Placement("D", INCREMENT))


@dataclass(frozen=True)
class Requirement:
    """
    State the desired output, independent route count and total capability budget.

    This local scenario always starts with raw integers. Each selected route
    must independently produce the output without sharing service placements.

    Attributes:
        output (str): Required semantic output type.
        routes (int): Number of disjoint service paths that must satisfy it.
        budget (int): Maximum summed capability cost across the composition.
    """

    output: str
    routes: int
    budget: int


ENVIRONMENTS = (Requirement("squared", 3, 4), Requirement("enriched", 2, 3), Requirement("enriched", 2, 3))


@dataclass(frozen=True)
class Settings:
    """
    Bound load, policy search, process overlap, observation and experiment duration.

    Command-line validation bounds these values before process creation. Timing
    is expressed in monotonic seconds. The shared in-flight window spans all
    routes; it is separate from each service's 64-job pending queue.

    Attributes:
        jobs (int): Producer jobs per route in each environment.
        window (int): In-flight multiplier; total credit is window times routes.
        work_seconds (float): Simulated I/O overhead per worker dispatch.
        tick (float): Interval between observation and supervision cycles.
        sustained (int): Consecutive high-backlog observations needed to adapt.
        cooldown (float): Minimum seconds between committed worker profiles.
        idle_seconds (float): Quiet interval required for interactive recovery.
        worker_limit (int): Live children per service, including rolling overlap.
        service_limit (int): Maximum distinct services in an admitted plan.
        overlap_limit (int): Maximum old and replacement service incarnations.
        candidate_limit (int): Maximum path combinations examined per selection.
        hard_minimum (float): Required expansion at service and worker boundaries.
        timeout (float): Deadline for each load round or lifecycle barrier.
    """

    jobs: int = 96
    window: int = 32
    work_seconds: float = 0.04
    tick: float = 0.02
    sustained: int = 3
    cooldown: float = 0.1
    idle_seconds: float = 0.2
    worker_limit: int = 4
    service_limit: int = 3
    overlap_limit: int = 5
    candidate_limit: int = 100
    hard_minimum: float = 1.0
    timeout: float = 25.0

    def soul_settings(self) -> soul.Settings:
        """
        Delegate timing, load and worker admission limits to Soul searching.

        Preserve Soul's default high-water mark while projecting the controls
        this example exposes. Producers can replace the job count to cover all
        routes without changing the worker policy settings.

        Returns:
            soul.Settings: Independent, immutable settings for shared Soul code.
        """
        return soul.Settings(
            jobs=self.jobs,
            work_seconds=self.work_seconds,
            tick=self.tick,
            sustained=self.sustained,
            cooldown=self.cooldown,
            idle_seconds=self.idle_seconds,
            worker_limit=self.worker_limit,
            hard_minimum=self.hard_minimum,
            timeout=self.timeout,
        )


@dataclass(frozen=True)
class Plan:
    """
    Carry compatible routes and the evidence used to admit their composition.

    The planner creates this value only after checking capability contracts,
    independent placements, steady cost, rolling capacity and exact expansion.

    Attributes:
        routes (tuple[tuple[Placement, ...], ...]): Ordered service stages for
            each independently sufficient input-to-output route.
        cost (int): Total steady-state cost of the admitted capabilities.
        expansion (float): Exact edge expansion of the composition graph.
    """

    routes: tuple[tuple[Placement, ...], ...]
    cost: int
    expansion: float

    @property
    def placements(self) -> dict[str, Placement]:
        """
        Return the single admitted capability for each service in the plan.

        Returns:
            dict[str, Placement]: Service names mapped to their assignments.
        """
        return {place.name: place for path in self.routes for place in path}

    @property
    def edges(self) -> set[tuple[str, str]]:
        """
        Describe the dataflow edges relayed by the parent through process pipes.

        Each path includes the logical input and output vertices. These edges
        describe computation routes; the parent transports each hop over IPC.

        Returns:
            set[tuple[str, str]]: Unique directed source/destination pairs.
        """
        return {edge for path in self.routes for edge in itertools.pairwise(["input", *(place.name for place in path), "output"])}

    def describe(self) -> list[list[str]]:
        """
        Render ordered capability assignments for lifecycle records.

        Returns:
            list[list[str]]: One list of service:capability labels per route.
        """
        return [[f"{place.name}:{place.capability.name}" for place in path] for path in self.routes]


def expansion(routes: tuple[tuple[Placement, ...], ...]) -> float:
    """
    Compute exact, undirected edge expansion of a small candidate composition.

    Include input, output and service vertices, deduplicate edges and enumerate
    each cut once up to its complement. Only the finite local catalog is used,
    keeping exponential cut enumeration small enough to inspect directly.

    Args:
        routes (tuple[tuple[Placement, ...], ...]): Nonempty candidate routes
            from the planner, each containing at least one placement.

    Returns:
        float: Minimum crossing-edge count divided by smaller-side vertex count.
    """
    edges = Plan(routes, 0, 0).edges
    vertices = sorted({vertex for edge in edges for vertex in edge})
    size = len(vertices)
    indexed = [(vertices.index(a), vertices.index(b)) for a, b in edges]
    return min(
        sum(((cut >> a) & 1) != ((cut >> b) & 1) for a, b in indexed) / min(cut.bit_count(), size - cut.bit_count())
        for cut in range(1, 1 << (size - 1))
    )


def natural_selection(requirement: Requirement, current: dict[str, Placement], settings: Settings) -> Plan:
    """
    Derive typed paths, admit bounded disjoint compositions, then rank cost and churn.

    Traverse the approved catalog using semantic input/output types. Compare
    combinations of independently sufficient routes without shared services.
    Reject cost, overlap and structural violations before ranking by cost and
    new incarnation count. Ties retain deterministic catalog traversal order.
    No process is started, stopped or mutated by this pure planning function.

    Args:
        requirement (Requirement): Desired output, independent routes and budget.
        current (dict[str, Placement]): Currently admitted service assignments;
            all count toward temporary overlap until their retirement.
        settings (Settings): Search, process and structural admission limits.

    Returns:
        Plan: The least costly feasible composition with minimal replacement
            count among equally costly candidates.

    Raises:
        RuntimeError: Search exhausts its candidate budget, or no composition
            satisfies all capability, cost, process and Cheeger constraints.
    """
    paths: list[tuple[Placement, ...]] = []

    def extend(kind: str, path: tuple[Placement, ...]) -> None:
        """
        Collect compatible paths without reusing a logical service.

        Args:
            kind (str): Semantic output currently available to the next stage.
            path (tuple[Placement, ...]): Stages selected so far from raw input.

        Returns:
            None: Completed routes are appended to the enclosing paths list.
        """
        if kind == requirement.output:
            paths.append(path)
            return
        if len(path) == settings.service_limit:
            return
        for place in CATALOG:
            if place.capability.input == kind and place.name not in {item.name for item in path}:
                extend(place.capability.output, (*path, place))

    extend("raw", ())
    feasible: list[tuple[int, int, Plan]] = []
    for count, routes in enumerate(itertools.combinations(paths, requirement.routes), 1):
        if count > settings.candidate_limit:
            raise RuntimeError("candidate budget exhausted before selection completed")
        places = [place for path in routes for place in path]
        names = {place.name for place in places}
        if len(names) != len(places) or len(names) > settings.service_limit:
            continue
        replacements = sum(current.get(place.name) != place for place in places)
        if len(current) + replacements > settings.overlap_limit:
            continue
        cost = sum(place.cost for place in places)
        value = expansion(routes)
        if cost <= requirement.budget and value >= settings.hard_minimum:
            feasible.append((cost, replacements, Plan(routes, cost, value)))
    if not feasible:
        raise RuntimeError("no composition satisfies the capability, route, cost, process-overlap and Cheeger requirements")
    return min(feasible, key=lambda candidate: candidate[:2])[2]


class Service:
    """
    Run Soul searching inside one revision-fenced capability incarnation.

    One service process owns this object and every worker in children. Its
    parent chooses the capability and composition revision; the local policy
    chooses interactive or batch execution within that assignment. Received
    job identities remain tracked until a new drained revision is adopted.

    Attributes:
        placement (Placement): Immutable capability assignment for this PID.
        revision (int): Parent plan revision accepted for new jobs.
        pipe (Connection): Duplex parent channel for commands and observations.
        settings (soul.Settings): Delegated worker policy and resource limits.
        children (dict[int, soul.Child]): Owned worker processes indexed by PID.
        pending (deque[tuple[int, int]]): Accepted jobs waiting for dispatch.
        inputs (dict[int, int]): Unfinished identities and their original values.
        accepted (set[int]): Every admitted identity in the active revision.
        active (soul.Profile): Currently committed worker profile.
        candidate (soul.Profile): Desired profile, possibly awaiting readiness.
        generation (int): Number of committed worker generations in this PID.
        high (int): Consecutive observations above the high-water mark.
        completed (int): Verified capability results across all revisions.
        previous (tuple[int, int]): Last reported backlog and completion counts.
        last_change (float): Monotonic time of the last profile commit.
        last_busy (float): Monotonic time of the most recent nonempty backlog.
        stopping (bool): Whether the parent has revoked new job admissions.
        idle_sent (bool): Whether this quiet period has been acknowledged.
    """

    placement: Placement
    revision: int
    pipe: Connection
    settings: soul.Settings
    children: dict[int, soul.Child]
    pending: deque[tuple[int, int]]
    inputs: dict[int, int]
    accepted: set[int]
    active: soul.Profile
    candidate: soul.Profile
    generation: int
    high: int
    completed: int
    previous: tuple[int, int]
    last_change: float
    last_busy: float
    stopping: bool
    idle_sent: bool

    def __init__(self, placement: Placement, revision: int, pipe: Connection, settings: Settings) -> None:
        """
        Initialize a bounded worker subtree with one interactive generation.

        Construction allocates supervision state. The first run-loop iteration
        starts the worker, so construction itself has no process side effects.

        Args:
            placement (Placement): Approved service identity and computation.
            revision (int): Initial parent plan revision.
            pipe (Connection): Child endpoint of the parent's control channel.
            settings (Settings): Runtime settings projected onto Soul's policy.
        """
        self.placement = placement
        self.revision = revision
        self.pipe = pipe
        self.settings = settings.soul_settings()
        self.children = {}
        self.pending = deque()
        self.inputs = {}
        self.accepted = set()
        self.active = soul.INTERACTIVE
        self.candidate = soul.INTERACTIVE
        self.generation = 0
        self.high = 0
        self.completed = 0
        self.previous = (0, 0)
        self.last_change = self.last_busy = time.monotonic()
        self.stopping = False
        self.idle_sent = False

    def run(self) -> None:
        """
        Execute the local receive, observe, adapt, dispatch and drain cycle.

        This has the same shape as Soul's AdaptiveService.run(). Nature adds a
        parent-selected capability and revision-fenced commands; the local
        worker policy still controls concurrency within that assignment.

        Returns:
            None: Stop was requested and the owned worker subtree finished.

        Raises:
            RuntimeError: A command, worker or admission contract fails.
            OSError: Communication fails while work or lifecycle receipts are exchanged.
        """
        while True:
            self.receive_workers()
            self.reap_workers()
            self.receive_commands()

            observation = self.observe(time.monotonic())
            self.propose_profile(observation)
            self.admit_profile()
            self.commit_profile(observation.time)
            self.dispatch()
            self.report_idle(observation)

            if self.finish_if_stopped():
                return
            time.sleep(self.settings.tick)

    def log(self, event: str, **details: Any) -> None:
        """
        Attach capability identity and plan revision to Soul lifecycle evidence.

        Args:
            event (str): Decision, observation or worker-lifecycle event name.
            **details (Any): JSON-serializable evidence for the event.

        Returns:
            None: The shared logger writes a JSON line to stdout.
        """
        soul.emit(event, service=self.placement.name, capability=self.placement.capability.name, revision=self.revision, **details)

    def admit_profile(self) -> None:
        """
        Check overlap and structural limits before starting candidate workers.

        Existing workers remain eligible while replacements start. Repeated
        calls do not start another copy of an already admitted generation.

        Returns:
            None: A pending profile has replacement workers starting within budget.

        Raises:
            RuntimeError: The candidate exceeds process, generation or Cheeger limits.
        """
        if self.stopping or (self.generation > 0 and self.candidate == self.active):
            return
        proposal = self.candidate
        if any(child.role == proposal.role and not child.retiring for child in self.children.values()):
            return
        if self.generation >= 9 or len(self.children) + proposal.workers > self.settings.worker_limit:
            raise RuntimeError("worker overlap or mutation budget exceeded")
        if soul.cheeger(proposal.workers) < max(self.settings.hard_minimum, proposal.target):
            raise RuntimeError("worker graph violates its structural bounds")
        for _ in range(proposal.workers):
            self.spawn_worker(proposal)

    def spawn_worker(self, profile: soul.Profile) -> None:
        """
        Start one admitted worker using this service's parent-selected capability.

        Args:
            profile (soul.Profile): Approved execution role and maximum batch size.

        Returns:
            None: The owned child is tracked while awaiting its readiness receipt.

        Raises:
            OSError: Pipe allocation or worker startup fails.
        """
        context = mp.get_context("spawn")
        parent, child_pipe = context.Pipe()
        process = context.Process(
            target=soul.worker,
            args=(child_pipe, profile, self.settings, self.placement.capability.compute),
            name=f"{self.placement.name}-{profile.role}",
        )
        try:
            process.start()
        except BaseException:
            parent.close()
            raise
        finally:
            child_pipe.close()
        assert process.pid is not None
        self.children[process.pid] = soul.Child(process, parent, profile.role)
        self.log("worker_spawned", worker=process.pid, role=profile.role, live=len(self.children))

    def commit_profile(self, now: float) -> None:
        """
        Transfer dispatch only after every worker in the candidate generation is ready.

        Old workers retain accepted jobs and become retiring. The first profile
        commit also announces readiness of the service incarnation to Nature.

        Args:
            now (float): Monotonic timestamp used for subsequent cooldown checks.

        Returns:
            None: The ready profile is committed, or its startup remains incomplete.
        """
        if self.stopping or (self.generation > 0 and self.candidate == self.active):
            return
        proposal = self.candidate
        replacements = [child for child in self.children.values() if child.role == proposal.role and not child.retiring]
        if len(replacements) != proposal.workers or not all(child.ready for child in replacements):
            return
        for child in self.children.values():
            child.retiring = child.role != proposal.role
        self.active = proposal
        self.generation += 1
        self.high = 0
        self.last_change = now
        self.log("committed", role=proposal.role, generation=self.generation, cheeger=soul.cheeger(proposal.workers))
        self.pipe.send(("profile", self.revision, proposal.role))
        if self.generation == 1:
            self.pipe.send(("ready", self.revision, None))

    def receive_workers(self) -> None:
        """
        Record available worker readiness or verify a completed capability batch.

        Returns:
            None: Child readiness and completed-work receipts advance.

        Raises:
            RuntimeError: A worker batch or capability result violates its contract.
            EOFError: A worker disconnects before its expected response.
        """
        for pid, child in self.children.items():
            if not child.pipe.poll() or child.stopping:
                continue
            kind, payload = child.pipe.recv()
            if kind == "ready":
                child.ready = True
                self.log("worker_ready", worker=pid, role=child.role)
            else:
                self.complete_batch(child, payload)

    def complete_batch(self, child: soul.Child, results: list[tuple[int, int]]) -> None:
        """
        Verify the selected capability's output before publishing revision-bound results.

        Args:
            child (soul.Child): Worker that owns the accepted batch.
            results (list[tuple[int, int]]): Returned job identities and computed values.

        Returns:
            None: Verified results reach Nature and the worker's batch is cleared.

        Raises:
            RuntimeError: Batch identities or capability results are incorrect.
        """
        if tuple(identity for identity, _ in results) != child.jobs:
            raise RuntimeError("worker completion does not match its admitted batch")
        for identity, value in results:
            if value != self.placement.capability.compute(self.inputs.pop(identity)):
                raise RuntimeError("capability returned an incorrect result")
            self.completed += 1
            self.pipe.send(("result", self.revision, (identity, value)))
        child.jobs = ()

    def reap_workers(self) -> None:
        """
        Join exited children only after a requested stop with no accepted batch remaining.

        Returns:
            None: Cleanly exited children are removed from the owned live population.

        Raises:
            RuntimeError: A worker exits without a clean drain and successful status.
        """
        for pid, child in list(self.children.items()):
            if child.process.is_alive():
                continue
            child.process.join()
            if not child.stopping or child.process.exitcode != 0 or child.jobs:
                raise RuntimeError("worker died before its accepted work drained")
            child.pipe.close()
            del self.children[pid]
            self.log("worker_joined", worker=pid, role=child.role)

    def command(self) -> None:
        """
        Accept only work for the active plan, and change revisions only when drained.

        Read one command. Jobs carry identity/value pairs, adopt commands carry
        a revision and stop revokes admission while accepted jobs finish.

        Returns:
            None: The command updates the local state and may acknowledge a plan.

        Raises:
            RuntimeError: A command is unknown, stale, duplicate or conflicts
                with accepted work or revoked admissions.
            EOFError: The parent closes the command endpoint.
        """
        kind, revision, payload = self.pipe.recv()
        if kind == "stop":
            self.stopping = True
        elif kind == "adopt":
            if self.stopping or self.inputs or revision < self.revision or (revision == self.revision and self.accepted):
                raise RuntimeError("plan revision cannot change with accepted work or move backward")
            self.revision = revision
            self.accepted.clear()
            self.idle_sent = False
            self.pipe.send(("adopted", revision, None))
        elif kind == "job":
            identity, value = payload
            if self.stopping or revision != self.revision or identity in self.accepted:
                raise RuntimeError("stale, duplicate or revoked job admission")
            self.accepted.add(identity)
            self.inputs[identity] = value
            self.pending.append((identity, value))
            self.idle_sent = False
        else:
            raise RuntimeError(f"unknown service command: {kind}")

    def dispatch(self) -> None:
        """
        Send bounded batches to eligible workers and stop children that have drained.

        Retiring children finish their accepted batch but receive no new work.
        During service shutdown, current workers finish the pending queue before
        receiving stop. Each eligible child holds at most one admitted batch.

        Returns:
            None: Worker pipes and accepted-batch records reflect this dispatch.

        Raises:
            OSError: A worker pipe closes before receiving its command.
        """
        for child in self.children.values():
            if child.stopping or child.jobs:
                continue
            should_stop = child.retiring or (self.stopping and not self.pending)
            if should_stop:
                child.pipe.send(None)
                child.stopping = True
            elif child.ready and child.role == self.active.role and self.pending:
                batch_size = min(self.active.batch, len(self.pending))
                batch = [self.pending.popleft() for _ in range(batch_size)]
                child.jobs = tuple(identity for identity, _ in batch)
                child.pipe.send(batch)

    def receive_commands(self) -> None:
        """
        Read available parent commands while pending-work admission still has capacity.

        Returns:
            None: Available jobs, revision changes and stop requests are processed.

        Raises:
            RuntimeError: A command is stale, duplicated or violates its contract.
        """
        while self.pipe.poll() and len(self.pending) < 64:
            self.command()

    def observe(self, now: float) -> soul.Observation:
        """
        Capture backlog, completion deltas and policy timing in a shared observation type.

        Args:
            now (float): Current monotonic observation timestamp.

        Returns:
            soul.Observation: One immutable snapshot for proposal and recovery checks.
        """
        backlog = len(self.inputs)
        if backlog:
            self.last_busy = now
        self.high = self.high + 1 if backlog >= self.settings.high_water else 0
        observation = soul.Observation(
            time=now,
            backlog=backlog,
            delta_backlog=backlog - self.previous[0],
            completed=self.completed,
            completed_delta=self.completed - self.previous[1],
            high=self.high,
            quiet=now - self.last_busy,
            elapsed=now - self.last_change,
        )
        if observation.delta_backlog or observation.completed_delta:
            self.log(
                "observed",
                backlog=backlog,
                delta_backlog=observation.delta_backlog,
                completed=self.completed,
                completed_delta=observation.completed_delta,
            )
        self.previous = (backlog, self.completed)
        return observation

    def workers_are_steady(self) -> bool:
        """
        Check whether a complete ready generation owns dispatch with no retirement pending.

        Returns:
            bool: True when every owned worker is ready and none is retiring or stopping.
        """
        return bool(self.children) and all(child.ready and not child.retiring and not child.stopping for child in self.children.values())

    def propose_profile(self, observation: soul.Observation) -> None:
        """
        Ask Soul searching for a worker profile once the preceding generation settles.

        Args:
            observation (soul.Observation): Current pressure, progress and timing evidence.

        Returns:
            None: The policy's approved choice becomes the candidate profile.
        """
        if self.stopping or self.generation == 0 or not self.workers_are_steady() or self.candidate != self.active:
            return
        self.candidate = soul.search_soul(
            self.active,
            observation.backlog,
            observation.high,
            observation.quiet,
            observation.elapsed,
            self.settings,
        )

    def report_idle(self, observation: soul.Observation) -> None:
        """
        Acknowledge quiet baseline recovery only after all accepted work and handoffs settle.

        Args:
            observation (soul.Observation): Work and quiet-time evidence from this cycle.

        Returns:
            None: A newly recovered service reports idle under its current plan revision.
        """
        if observation.backlog or not self.workers_are_steady() or self.active != soul.INTERACTIVE:
            return
        if observation.quiet >= self.settings.idle_seconds and not self.idle_sent:
            self.pipe.send(("idle", self.revision, self.completed))
            self.idle_sent = True

    def finish_if_stopped(self) -> bool:
        """
        Acknowledge parent shutdown only after the owned worker population is empty.

        Returns:
            bool: True when the service may return after sending its stop receipt.
        """
        if self.stopping and not self.children:
            self.pipe.send(("stopped", self.revision, self.completed))
            return True
        return False

    def close(self) -> None:
        """
        Reap owned workers on success, interruption or a failed contract.

        Send a stop sentinel where possible, then use bounded joins followed by
        termination and a final kill if necessary. Close every owned endpoint
        even when the normal readiness and drain protocol could not complete.

        Returns:
            None: Owned workers are joined and their communication is closed.
        """
        for child in self.children.values():
            try:
                child.pipe.send(None)
            except (OSError, EOFError):
                pass
        for child in self.children.values():
            child.process.join(timeout=2)
            if child.process.is_alive():
                child.process.terminate()
                child.process.join(timeout=2)
            if child.process.is_alive():
                child.process.kill()
                child.process.join()
            self.log("worker_cleaned", worker=child.process.pid, exitcode=child.process.exitcode)
            child.pipe.close()
        self.pipe.close()


def serve(placement: Placement, revision: int, pipe: Connection, settings: Settings) -> None:
    """
    Isolate one capability and guarantee cleanup of its Soul-managed worker subtree.

    This spawn entry point ignores terminal SIGINT and handles parent SIGTERM
    through stack unwinding. Cleanup runs whether the service stops normally,
    raises a contract error or is interrupted.

    Args:
        placement (Placement): Capability assigned to this service process.
        revision (int): Initial admitted composition revision.
        pipe (Connection): Child endpoint for parent commands and observations.
        settings (Settings): Delegated runtime and process limits.

    Returns:
        None: The service completed its run loop and joined its worker subtree.

    Raises:
        RuntimeError: The local service fails a command or worker contract.
        KeyboardInterrupt: Parent termination interrupts the service loop.
    """
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, soul.interrupt)
    service = Service(placement, revision, pipe, settings)
    try:
        service.run()
    finally:
        service.close()


@dataclass
class Incarnation:
    """
    Track one real service process, its contract and acknowledgements.

    Logical names can survive a mutation while this process identity changes.
    The parent retains every incarnation until final cleanup, including retired
    entries, so process ownership remains explicit across plan revisions.

    Attributes:
        placement (Placement): Service name and capability executed by this PID.
        process (BaseProcess): Spawned service process owned by Nature.
        pipe (Connection): Parent endpoint of the service control/data channel.
        flags (set[tuple[str, int]]): Observed lifecycle states keyed by revision.
    """

    placement: Placement
    process: BaseProcess
    pipe: Connection
    flags: set[tuple[str, int]] = field(default_factory=set)


@dataclass
class LoadRound:
    """
    Track one finite producer workload through the currently admitted composition.

    This ledger belongs to Nature. Service processes own their internal worker
    ledgers; a job here advances to the next service only after its result arrives.

    Attributes:
        requirement (Requirement): Output contract checked at the final stage.
        plan (Plan): Ordered routes admitted for this environment.
        generator (BaseProcess): Owned producer sending work to the parent.
        incoming (Connection): Read endpoint for producer work and end-of-input.
        total (int): Expected distinct producer identities in the round.
        started (float): Monotonic start time for the round deadline and rate.
        inflight (dict[int, tuple[int, int]]): Each unfinished job's route and stage.
        completed (set[int]): Identities whose final results have been verified.
        accepted (int): Count of sequential producer jobs admitted so far.
        ending (bool): Whether the producer's end-of-input sentinel arrived.
    """

    requirement: Requirement
    plan: Plan
    generator: BaseProcess
    incoming: Connection
    total: int
    started: float = field(default_factory=time.monotonic)
    inflight: dict[int, tuple[int, int]] = field(default_factory=dict)
    completed: set[int] = field(default_factory=set)
    accepted: int = 0
    ending: bool = False


class Nature:
    """
    Own composition revisions, root-routed dataflow and service survival decisions.

    Selection and placement run in this parent process. Child services own
    their worker pools and cannot change their own capability assignment.
    Complete one load round before replacing its admitted composition.

    Attributes:
        settings (Settings): Policy, runtime and admission budgets.
        context (mp.context.SpawnContext): Spawn context used for direct children.
        active (dict[str, Incarnation]): Current incarnation of each service.
        created (list[Incarnation]): All service processes retained for cleanup.
        generators (list[BaseProcess]): Producer processes owned by this parent.
        plan (Plan | None): Current admitted composition, initially absent.
        revision (int): Monotonically increasing composition revision.
    """

    settings: Settings
    context: mp.context.SpawnContext
    active: dict[str, Incarnation]
    created: list[Incarnation]
    generators: list[BaseProcess]
    plan: Plan | None
    revision: int

    def __init__(self, settings: Settings) -> None:
        """
        Begin with an empty population and retain ownership of every created child.

        Args:
            settings (Settings): Validated configuration for this experiment.
        """
        self.settings = settings
        self.context = mp.get_context("spawn")
        self.active = {}
        self.created = []
        self.generators = []
        self.plan = None
        self.revision = 0

    def run(self) -> int:
        """
        Apply each environment, verify its work and retire the final population.

        The selected services run concurrently during each load round. The
        parent advances the environment only after verified completion and
        worker recovery, preserving the drained boundary needed by apply().

        Returns:
            int: Total verified producer jobs across every environment.

        Raises:
            RuntimeError: Selection, workload verification or a child contract fails.
            TimeoutError: A lifecycle barrier expires.
        """
        completed = 0
        for requirement in ENVIRONMENTS:
            plan = self.apply(requirement)
            completed += self.load(requirement, plan)
        self.retire_population()
        return completed

    def retire_population(self) -> None:
        """
        Gracefully retire all survivors after the last environment finishes.

        Returns:
            None: All active service subtrees are joined and the population is empty.

        Raises:
            RuntimeError: A survivor fails its clean shutdown contract.
            TimeoutError: A survivor exceeds the retirement deadline.
        """
        for member in self.active.values():
            self.retire(member, "experiment complete")
        self.active.clear()

    def messages(self, members: list[Incarnation]) -> list[tuple[Incarnation, str, int, Any]]:
        """
        Consume bounded available messages and reject unexpected service failure.

        Read at most 128 available messages per service, preserving each pipe's
        ordering. Record lifecycle acknowledgements by revision for barriers.
        Result messages retain their identity/value payload for routing checks.

        Args:
            members (list[Incarnation]): Live services whose pipes may be read.

        Returns:
            list[tuple[Incarnation, str, int, Any]]: Source incarnation, message
                kind, plan revision and payload for each available record.

        Raises:
            RuntimeError: A service exits or closes its pipe before a clean stop.
        """
        result = []
        for member in members:
            for _ in range(128):
                if not member.pipe.poll():
                    break
                try:
                    kind, revision, payload = member.pipe.recv()
                except EOFError:
                    if ("stopped", self.revision) in member.flags:
                        break
                    raise RuntimeError(f"{member.placement.name} closed before a clean stop") from None
                member.flags.add((kind if kind != "profile" else payload, revision))
                result.append((member, kind, revision, payload))
            if member.process.exitcode is not None and ("stopped", self.revision) not in member.flags:
                raise RuntimeError(f"{member.placement.name} exited unexpectedly")
        return result

    def barrier(self, members: list[Incarnation], kind: str, revision: int) -> None:
        """
        Wait for explicit readiness, revision or shutdown acknowledgements with a deadline.

        Barriers run between load rounds, where no job results should remain.
        Empty member lists complete immediately, allowing unchanged populations
        to reuse the same admission procedure as a mutation.

        Args:
            members (list[Incarnation]): Services that must acknowledge the state.
            kind (str): Required lifecycle acknowledgement, such as ready.
            revision (int): Plan revision to which each acknowledgement belongs.

        Returns:
            None: All members have acknowledged the requested state and revision.

        Raises:
            RuntimeError: Work crosses the drained boundary or a service fails.
            TimeoutError: The configured lifecycle deadline expires.
        """
        deadline = time.monotonic() + self.settings.timeout
        while not all((kind, revision) in member.flags for member in members):
            for _, event, _, _ in self.messages(members):
                if event == "result":
                    raise RuntimeError("work crossed a drained plan boundary")
            if time.monotonic() > deadline:
                raise TimeoutError(f"{kind} barrier exceeded its deadline")
            time.sleep(self.settings.tick)

    def apply(self, requirement: Requirement) -> Plan:
        """
        Select a composition, prepare its population, commit routing and retire exclusions.

        These phases run after the preceding load round has drained. Read the
        steps here before following the helpers for spawn and receipt details.

        Args:
            requirement (Requirement): Outcome and budget for the new environment.

        Returns:
            Plan: The committed composition after excluded services have exited.

        Raises:
            RuntimeError: Selection, a child or a lifecycle contract fails.
            TimeoutError: A readiness, revision or retirement barrier expires.
        """
        plan = self.select_plan(requirement)
        next_members, replacements = self.prepare_population(plan)
        self.barrier(replacements, "ready", self.revision)
        self.adopt_revision(list(next_members.values()))
        retiring = self.commit_plan(plan, next_members)
        self.retire_exclusions(retiring)
        return plan

    def select_plan(self, requirement: Requirement) -> Plan:
        """
        Derive an admitted composition and publish its evidence under a new revision.

        Args:
            requirement (Requirement): Required output, independent routes and cost ceiling.

        Returns:
            Plan: The feasible composition chosen by the pure Natural Selection planner.

        Raises:
            RuntimeError: No candidate satisfies the requirement within all limits.
        """
        current = {name: member.placement for name, member in self.active.items()}
        plan = natural_selection(requirement, current, self.settings)
        self.revision += 1
        soul.emit(
            "selection",
            revision=self.revision,
            output=requirement.output,
            required_routes=requirement.routes,
            budget=requirement.budget,
            cost=plan.cost,
            cheeger=plan.expansion,
            routes=plan.describe(),
        )
        return plan

    def prepare_population(self, plan: Plan) -> tuple[dict[str, Incarnation], list[Incarnation]]:
        """
        Preserve useful service PIDs and start only changed or newly required assignments.

        Args:
            plan (Plan): Admitted assignments with their rolling overlap already checked.

        Returns:
            tuple[dict[str, Incarnation], list[Incarnation]]: Next population keyed
                by service name, followed by just the incarnations awaiting readiness.
        """
        next_members: dict[str, Incarnation] = {}
        replacements = []
        for name, placement in plan.placements.items():
            previous = self.active.get(name)
            if previous is not None and previous.placement == placement:
                next_members[name] = previous
                soul.emit(
                    "survived",
                    service=name,
                    service_pid=previous.process.pid,
                    revision=self.revision,
                    capability=placement.capability.name,
                )
            else:
                member = self.start_incarnation(placement, previous)
                next_members[name] = member
                replacements.append(member)
        return next_members, replacements

    def start_incarnation(self, placement: Placement, previous: Incarnation | None) -> Incarnation:
        """
        Spawn one admitted capability and track its process before awaiting readiness.

        Args:
            placement (Placement): Service name and capability for the new process.
            previous (Incarnation | None): Old incarnation when this is a mutation.

        Returns:
            Incarnation: The owned replacement with its parent-side command endpoint.

        Raises:
            OSError: Process or pipe creation fails.
        """
        parent, child = self.context.Pipe()
        process = self.context.Process(
            target=serve,
            args=(placement, self.revision, child, self.settings),
            name=f"{placement.name}-r{self.revision}",
        )
        try:
            process.start()
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        member = Incarnation(placement, process, parent)
        self.created.append(member)
        soul.emit(
            "mutated" if previous else "born",
            service=placement.name,
            service_pid=process.pid,
            previous_pid=previous.process.pid if previous else None,
            capability=placement.capability.name,
            revision=self.revision,
            live=sum(item.process.is_alive() for item in self.created),
        )
        return member

    def adopt_revision(self, members: list[Incarnation]) -> None:
        """
        Fence new work to the admitted revision on every survivor and replacement.

        Args:
            members (list[Incarnation]): Ready incarnations in the next population.

        Returns:
            None: Every member acknowledged the new revision while drained.

        Raises:
            RuntimeError: A service rejects adoption or reports undrained work.
            TimeoutError: Adoption exceeds the lifecycle deadline.
        """
        for member in members:
            member.pipe.send(("adopt", self.revision, None))
        self.barrier(members, "adopted", self.revision)

    def commit_plan(self, plan: Plan, next_members: dict[str, Incarnation]) -> list[Incarnation]:
        """
        Publish edge changes and transfer active routing to the acknowledged population.

        Args:
            plan (Plan): Composition whose members have adopted the current revision.
            next_members (dict[str, Incarnation]): Ready service assignments keyed by name.

        Returns:
            list[Incarnation]: Excluded old incarnations that still require retirement.
        """
        old_edges = self.plan.edges if self.plan else set()
        retiring = [member for name, member in self.active.items() if next_members.get(name) is not member]
        soul.emit(
            "plan_committed",
            revision=self.revision,
            routes=plan.describe(),
            cheeger=plan.expansion,
            added_edges=sorted(plan.edges - old_edges),
            removed_edges=sorted(old_edges - plan.edges),
            services={name: member.process.pid for name, member in next_members.items()},
        )
        self.active = next_members
        self.plan = plan
        return retiring

    def retire_exclusions(self, members: list[Incarnation]) -> None:
        """
        Join old incarnations once their replacements and new routing are committed.

        Args:
            members (list[Incarnation]): Excluded old service processes.

        Returns:
            None: Each excluded service has exited with an explicit selection reason.

        Raises:
            RuntimeError: An excluded service fails its clean shutdown contract.
            TimeoutError: Retirement exceeds the lifecycle deadline.
        """
        for member in members:
            reason = "capability replaced" if member.placement.name in self.active else "excluded by outcome and cost budget"
            self.retire(member, reason)

    def retire(self, member: Incarnation, reason: str) -> None:
        """
        Stop admissions, drain the service subtree and verify the process has exited.

        Retirement follows a completed load round. Wait for the service's stop
        acknowledgement, join its PID and record the concrete reason for its
        death. The service itself remains responsible for joining its workers.

        Args:
            member (Incarnation): Previously admitted process to retire.
            reason (str): Human-readable selection or shutdown explanation.

        Returns:
            None: The service process exited successfully and its pipe is closed.

        Raises:
            RuntimeError: Accepted work remains or the service exits uncleanly.
            TimeoutError: Retirement exceeds the configured lifecycle deadline.
            EOFError: The service closes its pipe before acknowledging stop.
        """
        member.pipe.send(("stop", self.revision, None))
        # Excluded incarnations retain their last admitted revision while draining.
        deadline = time.monotonic() + self.settings.timeout
        stopped = False
        while not stopped:
            if member.pipe.poll(self.settings.tick):
                kind, _, _ = member.pipe.recv()
                if kind == "result":
                    raise RuntimeError("retirement began before accepted work drained")
                stopped = kind == "stopped"
            if time.monotonic() > deadline:
                raise TimeoutError("service retirement exceeded its deadline")
        member.process.join(timeout=3)
        if member.process.exitcode != 0:
            raise RuntimeError("service failed to exit cleanly")
        member.pipe.close()
        soul.emit(
            "retired", service=member.placement.name, service_pid=member.process.pid, revision=self.revision, reason=reason, exitcode=0
        )

    def load(self, requirement: Requirement, plan: Plan) -> int:
        """
        Run the bounded admit, route, recover and verify cycle for one environment.

        Args:
            requirement (Requirement): Outcome against which final results are checked.
            plan (Plan): Currently committed routes and capability assignments.

        Returns:
            int: Distinct producer jobs verified before the population became idle.

        Raises:
            RuntimeError: Input, routing, output, adaptation or deadline checks fail.
            OSError: A producer or service pipe fails during the round.
        """
        round_state = self.start_round(requirement, plan)
        try:
            while not self.round_finished(round_state):
                self.admit_jobs(round_state)
                self.route_results(round_state)
                self.check_round_health(round_state)
                time.sleep(self.settings.tick)
            return self.verify_round(round_state)
        finally:
            round_state.incoming.close()

    def start_round(self, requirement: Requirement, plan: Plan) -> LoadRound:
        """
        Start a finite producer and reset this revision's load and recovery evidence.

        Args:
            requirement (Requirement): Required output for the round.
            plan (Plan): Routes over which producer identities will be distributed.

        Returns:
            LoadRound: The owned producer endpoint and initially empty routing ledger.

        Raises:
            OSError: The producer or its pipe cannot be created.
        """
        incoming, output = self.context.Pipe(duplex=False)
        total = self.settings.jobs * len(plan.routes)
        producer_settings = replace(self.settings.soul_settings(), jobs=total)
        generator = self.context.Process(target=soul.producer, args=([output], producer_settings))
        try:
            generator.start()
        except BaseException:
            incoming.close()
            raise
        finally:
            output.close()
        self.generators.append(generator)
        for member in self.active.values():
            member.flags.discard(("idle", self.revision))
            member.flags.discard(("batch", self.revision))
        soul.emit(
            "load_started",
            revision=self.revision,
            producer=generator.pid,
            services=[member.process.pid for member in self.active.values()],
        )
        return LoadRound(requirement, plan, generator, incoming, total)

    def admit_jobs(self, round_state: LoadRound) -> None:
        """
        Admit ordered producer jobs only while the shared in-flight budget has room.

        Args:
            round_state (LoadRound): Current input contract, routes and work ledger.

        Returns:
            None: Available jobs are assigned to first stages, or backpressure defers input.

        Raises:
            RuntimeError: The producer sends an invalid identity, value or job count.
        """
        routes = round_state.plan.routes
        capacity = self.settings.window * len(routes)
        while not round_state.ending and len(round_state.inflight) < capacity and round_state.incoming.poll():
            job = round_state.incoming.recv()
            if job is None:
                round_state.ending = True
                break
            identity, value = job
            if identity != round_state.accepted or value != identity + 1 or identity >= round_state.total:
                raise RuntimeError("producer violated its input contract")
            round_state.accepted += 1
            route = identity % len(routes)
            round_state.inflight[identity] = (route, 0)
            self.send_job(self.active[routes[route][0].name], identity, value)

    def send_job(self, member: Incarnation, identity: int, value: int) -> None:
        """
        Revoke stale idle evidence and send one stage's work under the current revision.

        Args:
            member (Incarnation): Admitted service responsible for this stage.
            identity (int): Producer identity preserved throughout the route.
            value (int): Raw input or verified output from the preceding stage.

        Returns:
            None: The stage command is sent and this service must report idle again.
        """
        member.flags.discard(("idle", self.revision))
        member.pipe.send(("job", self.revision, (identity, value)))

    def route_results(self, round_state: LoadRound) -> None:
        """
        Consume current-revision observations and advance completed stages.

        Args:
            round_state (LoadRound): In-flight route positions and completion ledger.

        Returns:
            None: Available results have advanced or completed their jobs.

        Raises:
            RuntimeError: An observation belongs to a stale plan or a result is invalid.
        """
        for member, kind, revision, payload in self.messages(list(self.active.values())):
            if revision != self.revision:
                raise RuntimeError("observation belongs to a stale plan")
            if kind == "result":
                member.flags.discard(("idle", self.revision))
                identity, value = payload
                self.complete_stage(round_state, member, identity, value)

    def complete_stage(self, round_state: LoadRound, member: Incarnation, identity: int, value: int) -> None:
        """
        Check stage ownership, then forward the result or verify the final outcome.

        Args:
            round_state (LoadRound): Current routes, requirement and work ledger.
            member (Incarnation): Service that returned the result.
            identity (int): Producer job identity carried by the result.
            value (int): Computed output of this stage.

        Returns:
            None: The next stage owns the job or its final output releases in-flight credit.

        Raises:
            RuntimeError: Stage ownership, result value or completion uniqueness fails.
        """
        route, stage = round_state.inflight[identity]
        path = round_state.plan.routes[route]
        if path[stage] != member.placement:
            raise RuntimeError("result came from the wrong stage")
        if stage + 1 < len(path):
            self.send_job(self.active[path[stage + 1].name], identity, value)
            round_state.inflight[identity] = (route, stage + 1)
            return
        expected = (identity + 1) ** 2 + (round_state.requirement.output == "enriched")
        if value != expected or identity in round_state.completed:
            raise RuntimeError("wrong or duplicate final outcome")
        round_state.completed.add(identity)
        del round_state.inflight[identity]

    def round_finished(self, round_state: LoadRound) -> bool:
        """
        Check that input ended, jobs completed and every service restored its baseline.

        Args:
            round_state (LoadRound): Current producer and unfinished-work evidence.

        Returns:
            bool: True only after complete input, routing and worker recovery.
        """
        return (
            round_state.ending
            and not round_state.inflight
            and all(("idle", self.revision) in member.flags for member in self.active.values())
        )

    def check_round_health(self, round_state: LoadRound) -> None:
        """
        Bound the load round and reject producer failure while routing continues.

        Args:
            round_state (LoadRound): Producer process and monotonic round start.

        Returns:
            None: The producer has not failed and the round remains within its deadline.

        Raises:
            RuntimeError: The round times out or the producer exits unsuccessfully.
        """
        expired = time.monotonic() - round_state.started > self.settings.timeout
        if expired or round_state.generator.exitcode not in (None, 0):
            raise RuntimeError("load round deadline or producer failure")

    def verify_round(self, round_state: LoadRound) -> int:
        """
        Verify exact completion and local adaptation before publishing the round receipt.

        Args:
            round_state (LoadRound): Finished round with all result identities recorded.

        Returns:
            int: Exact number of verified producer jobs in this environment.

        Raises:
            RuntimeError: The producer, completion ledger or adaptation evidence is incomplete.
        """
        round_state.generator.join(timeout=3)
        if round_state.generator.exitcode != 0 or round_state.completed != set(range(round_state.total)):
            raise RuntimeError("load did not complete exactly once")
        if any(("batch", self.revision) not in member.flags for member in self.active.values()):
            raise RuntimeError("load did not demonstrate adaptation in every selected service")
        elapsed = time.monotonic() - round_state.started
        soul.emit(
            "round_verified",
            revision=self.revision,
            completed=round_state.total,
            output=round_state.requirement.output,
            elapsed=round(elapsed, 3),
            rate=round(round_state.total / elapsed, 2),
            services={name: member.process.pid for name, member in self.active.items()},
        )
        return round_state.total

    def close(self) -> None:
        """
        Bound cleanup of producers and all service subtrees after success or interruption.

        Request normal service stops first. Join direct children with bounded
        waits, then terminate unresponsive processes so their cleanup handlers
        run. A final kill prevents an unresponsive direct child from lingering.

        Returns:
            None: Direct children are joined and service endpoints are closed.
        """
        for member in self.created:
            if member.process.is_alive():
                try:
                    member.pipe.send(("stop", self.revision, None))
                except OSError:
                    pass
        for process in [*self.generators, *(member.process for member in self.created)]:
            process.join(timeout=3)
            if process.is_alive():
                process.terminate()
                process.join(timeout=6)
            if process.is_alive():
                process.kill()
                process.join()
        for member in self.created:
            member.pipe.close()


def parse_settings() -> Settings:
    """
    Parse and validate command-line controls before creating the parent population.

    Returns:
        Settings: Finite configuration within the local experiment's supported limits.

    Raises:
        SystemExit: Argparse handles help or rejects unsupported settings.
    """
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for item in fields(Settings):
        parser.add_argument("--" + item.name.replace("_", "-"), type=type(item.default), default=item.default)
    settings = Settings(**vars(parser.parse_args()))
    if any(not math.isfinite(value) or value <= 0 for value in vars(settings).values()):
        parser.error("use finite positive settings")
    if not 96 <= settings.jobs <= 2000 or not 16 <= settings.window <= 64:
        parser.error("use 96 through 2000 jobs per route and a window of 16 through 64")
    if settings.worker_limit > 6 or settings.service_limit > 4 or settings.overlap_limit > 8 or settings.hard_minimum > 1:
        parser.error("limits: six workers, four steady services, eight overlapping services; initial worker Cheeger is 1")
    return settings


def main() -> None:
    """
    Configure Nature, execute its environments and always close the owned population.

    Read Nature.run() next for environment order, Nature.apply() for mutation
    handoffs, Nature.load() for dataflow and Service.run() for local adaptation.

    Returns:
        None: Every environment finished and cleanup preceded the success receipt.

    Raises:
        SystemExit: Command-line help or validation ends the invocation.
        RuntimeError: Planning, work or a child contract fails.
        TimeoutError: A lifecycle barrier exceeds its deadline.
        KeyboardInterrupt: Cancellation unwinds through population cleanup.
    """
    settings = parse_settings()
    signal.signal(signal.SIGTERM, soul.interrupt)
    nature = Nature(settings)
    try:
        completed = nature.run()
    except (RuntimeError, TimeoutError, EOFError, OSError) as error:
        soul.emit("nature_blocked", revision=nature.revision, reason=str(error))
        raise
    finally:
        nature.close()
    soul.emit("nature_success", environments=len(ENVIRONMENTS), completed=completed, all_joined=True, population=0)


if __name__ == "__main__":
    main()
