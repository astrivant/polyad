"""
Choose services for a changing task, then let each service adjust to its load.

This demo sends integers through real Python processes. At first, each job
must return x*x: an input of 3 must produce 9. Later, the task changes to
x*x + 1, so that same input must produce 10. The parent process chooses which
services to run and how to connect their processing steps. Each service then
adjusts its own number of worker processes as work arrives and finishes.

The parent makes its choices with Natural Selection. Each service adjusts its
workers with Soul searching, imported from soul.py. These are two decisions:
which functions the application needs, and how to run each function under load.

Terms used in the code
---------------------
A capability is a function a service can run, such as square() or increment().
A placement assigns one capability to a named service, such as B running square().
A route lists the services a job passes through, such as B followed by D. A plan
contains the chosen services and routes; together they form the composition.
Each environment is one round of the demo with a required result and a budget.

The labels raw, squared and enriched describe what an integer means at a step:
raw is the original x, squared is x*x, and enriched is x*x + 1. These labels
let the planner check whether one function's output fits the next function's
input. That agreement is the input/output contract. For example, increment()
expects squared input; sending it raw input would produce the wrong final result.

A worker profile chooses one interactive worker or three batch workers. Changing
profiles replaces child workers while their service stays alive. Replacing a
service itself creates a new process instance, called an incarnation in the
code. Every plan has a version number, called its revision, which travels with
job messages so a service can reject work sent for the wrong plan.

Run from the repository root with the local SDK installed:

    python -m pip install ./pkg/polyad-types ./pkg/polyad-sdk
    python demo/nature.py
    python demo/nature.py --jobs 128
    python demo/nature.py --help

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
Natural Selection chooses WHICH functions run in which services and routes.
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
the required result and the service routes around that machinery.

Environment 1: all three can survive
-----------------------------------
The required result is x*x. Three independent routes are required, with a total
cost of at most 4. Costs are declared units for comparing plans in this demo,
rather than measured CPU or memory. A and B cost 1 each; C costs 2. All three fit:

                    +--> A: square --+
    input ----------+--> B: square --+----------> verified output
                    +--> C: square --+

    Composition Cheeger = 1.5. Three service processes are alive.

The producer creates pressure. Every service uses Soul searching to roll from
one interactive worker to three batch workers, finish their work, then return
to one interactive worker:

    service PID stays alive
    +-- interactive       +-- batch-1       +-- interactive (new worker PID)
                          +-- batch-2
                          +-- batch-3

Environment 2: the requirements change
--------------------------------------
Now the result must be x*x + 1, with two independent routes and a cost ceiling
of 3. The catalog lists the functions each service can run. A can run fused(),
which squares the input and adds one in a single step. D can add one to a
value that B or C has already squared. B and C can only square values.

                    +--> A': square-plus-one --------+
    input ----------+                               +--> verified output
                    +--> B: square --> D: increment -+

    Composition Cheeger = 1.0. A', B and D are alive; old A and C have exited.

The planner tries routes whose output labels match the next step's input label.
It combines routes that share no service, then checks cost, process counts and
the minimum Cheeger value. The process limit includes old and new processes
alive together during replacement. Among plans that pass, it chooses the lowest
cost, then the fewest new service processes.

    A: can mutate -> a ready replacement A' gets a new PID and capability.
    B: stays useful -> its service PID survives and feeds the new D stage.
    C: stays fixed -> B + D is cheaper than C + D; C loses its place and dies.
    D: fills a missing capability -> a new service process is born.

"Complacent" describes C's fixed choice of functions. B also stays unchanged,
but still fits the result and budget, so it survives. Retirement or death means
finishing accepted work, stopping the process and waiting for its exit. Mutation
means replacing a service with one running a different function from the catalog.

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
graph connecting the services. Cheeger measures connections across the sparsest
split of a graph, relative to the number of vertices on its smaller side. The
same configured minimum applies separately to the service graph and each
service's worker graph.
Measured completion counts, backlog deltas and rates accompany the structural
measurements; correctness and completion before the deadline verify each round.

Admission and retirement
------------------------
Between environments, all jobs finish before the parent starts replacement
services. It waits for them to be ready, asks every selected service to accept
the new plan's version number, switches the routes, then stops the old services
that are no longer selected. The live-service limit includes the overlap:

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
import sys
import time
from collections import deque
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING

if not __package__:
    # Direct script execution starts with demo/ on sys.path. Resolve the package
    # from this file, not the working directory, so spawned children can import it.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from demo import soul  # noqa: E402  # Package import follows the direct-script path bootstrap.

if TYPE_CHECKING:
    from collections.abc import Callable
    from multiprocessing.connection import Connection
    from multiprocessing.process import BaseProcess
    from typing import Any


def square(value: int) -> int:
    """
    Square the producer's input, such as turning 3 into 9.

    Args:
        value (int): Original integer sent by the producer.

    Returns:
        int: The input multiplied by itself, labeled squared by the planner.
    """
    return value * value


def increment(value: int) -> int:
    """
    Add one to the result of an earlier square() step.

    For an original input of 3, square() produces 9 and this function returns
    10. In a two-service route, B squares the input and D runs this function.

    Args:
        value (int): Squared integer returned by the preceding service.

    Returns:
        int: The squared value plus one, labeled enriched by the planner.
    """
    return value + 1


def fused(value: int) -> int:
    """
    Square the original input and add one in a single processing step.

    For an input of 3, return 10. The parent can run this function in service A
    or send the job through B's square() and D's increment(). Both routes give
    the same answer, so the planner can compare their cost and process needs.

    Args:
        value (int): Original integer sent by the producer.

    Returns:
        int: The square of the input plus one.
    """
    return value * value + 1


@dataclass(frozen=True)
class Capability:
    """
    Describe a function a service can run and the data it accepts and produces.

    All values in this demo are Python integers, but their meanings differ:
    raw means x, squared means x*x, and enriched means x*x + 1. The planner
    matches these labels to connect functions in the correct order. For
    example, increment() accepts squared input and produces enriched output.

    Attributes:
        name (str): Function label used in plans and logs, such as square.
        input (str): Meaning of the expected input: raw, squared or enriched.
        output (str): Meaning of the result, using the same labels as input.
        compute (Callable[[int], int]): Function called by a worker. It must be
            importable by a new process and return its result without side effects.
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
    Assign a function and a declared cost to a named service.

    The catalog contains two choices for A: square() and fused(). A plan can
    select only one of them. Changing that choice replaces A's process.

    Attributes:
        name (str): Service name, such as A, retained when its process is replaced.
        capability (Capability): Function this service's workers will run.
        cost (int): Budget units charged while this assignment is selected.
            These are example costs, not measured CPU or memory usage.
    """

    name: str
    capability: Capability
    cost: int = 1


CATALOG = (Placement("A", SQUARE), Placement("A", FUSED), Placement("B", SQUARE), Placement("C", SQUARE, 2), Placement("D", INCREMENT))


@dataclass(frozen=True)
class Requirement:
    """
    Specify the required result, number of separate routes and cost limit.

    Every route starts with the producer's original integer and must produce
    the required result. Routes cannot share a service. For example, producing
    x*x + 1 along two routes needs both A alone and B followed by D.

    Attributes:
        output (str): Required result label: squared for x*x or enriched for x*x + 1.
        routes (int): Number of routes, with no service shared between them.
        budget (int): Maximum sum of the chosen Placement costs across all routes.
    """

    output: str
    routes: int
    budget: int


ENVIRONMENTS = (Requirement("squared", 3, 4), Requirement("enriched", 2, 3), Requirement("enriched", 2, 3))


@dataclass(frozen=True)
class Settings:
    """
    Configure job counts, adaptation timing, process limits and timeouts.

    parse_settings() checks these values before any process starts. Durations
    are in seconds and use a monotonic clock, so wall-clock adjustments do not
    affect deadlines. The in-flight limit counts unfinished jobs across all
    routes; each service also has its own queue of up to 64 waiting jobs.

    Attributes:
        jobs (int): Producer jobs per route in each environment.
        window (int): Unfinished jobs allowed per route on average. The shared
            limit is window times the number of routes.
        work_seconds (float): Simulated I/O overhead per worker dispatch.
        tick (float): Interval between observation and supervision cycles.
        sustained (int): Consecutive high-backlog observations needed to adapt.
        cooldown (float): Minimum seconds between switches of worker profile.
        idle_seconds (float): Seconds without work before returning to one worker.
        worker_limit (int): Maximum live workers per service, including old
            workers finishing jobs while replacement workers start.
        service_limit (int): Maximum named services selected in a plan.
        overlap_limit (int): Maximum service processes alive during replacement,
            counting both old and new processes.
        candidate_limit (int): Maximum path combinations examined per selection.
        hard_minimum (float): Minimum Cheeger value, checked separately for the
            graph of services and each service's graph of workers.
        timeout (float): Seconds allowed for a load round or a wait for service
            readiness, plan acknowledgement or shutdown.
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
        Build the settings used by the worker and adaptation code in soul.py.

        Copy the controls shared by both demos. Keep soul.py's default backlog
        threshold for switching to batch workers. start_round() separately
        increases the producer's job count to supply every selected route.

        Returns:
            soul.Settings: A new, immutable settings object for the reused code.
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
    Store the chosen service routes, their total cost and their Cheeger value.

    Each route is an ordered series of processing steps. For example, the
    square-plus-one task can use a plan with two routes: A alone, and B then D.
    natural_selection() returns a plan after checking its costs and limits.

    Attributes:
        routes (tuple[tuple[Placement, ...], ...]): Ordered service stages for
            each route from the original input to the required final result.
        cost (int): Sum of the declared costs of the selected placements.
        expansion (float): Exact Cheeger value for the graph of these routes.
    """

    routes: tuple[tuple[Placement, ...], ...]
    cost: int
    expansion: float

    @property
    def placements(self) -> dict[str, Placement]:
        """
        Look up each selected service's function and cost by its name.

        Returns:
            dict[str, Placement]: Service names mapped to their assignments.
        """
        return {place.name: place for path in self.routes for place in path}

    @property
    def edges(self) -> set[tuple[str, str]]:
        """
        List the connections a job follows from input through services to output.

        A route through B then D has edges input -> B, B -> D and D -> output.
        The parent carries each message over process pipes, including B's
        result on its way to D. Input and output are also vertices in this graph.

        Returns:
            set[tuple[str, str]]: Unique directed source/destination pairs.
        """
        return {edge for path in self.routes for edge in itertools.pairwise(["input", *(place.name for place in path), "output"])}

    def describe(self) -> list[list[str]]:
        """
        Format routes for logs, such as [B:square, D:increment].

        Returns:
            list[list[str]]: One list of service:capability labels per route.
        """
        return [[f"{place.name}:{place.capability.name}" for place in path] for path in self.routes]


def expansion(routes: tuple[tuple[Placement, ...], ...]) -> float:
    """
    Calculate the Cheeger value for a proposed set of service routes.

    Include one vertex per service plus input and output, and ignore edge
    direction for this calculation. For each split into two nonempty groups,
    divide the number of crossing edges by the number of vertices in the smaller
    group. Return the smallest ratio. A lower value indicates a sparser
    connection between parts of the graph.

    This checks every distinct split, which is practical for this small demo
    but becomes expensive as the number of vertices grows.

    Args:
        routes (tuple[tuple[Placement, ...], ...]): Proposed routes, each with
            at least one service. The collection must also be nonempty.

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
    Choose service routes that produce the required result within the limits.

    First build routes by matching function labels: square() turns raw input
    into squared output, which increment() can accept. Then combine the required
    number of routes, checking that they share no service and fit the cost,
    process and Cheeger limits. Count old and new processes together when
    checking the space needed for replacements.

    Prefer the lowest total cost, then the fewest new service processes. Equal
    choices follow catalog order. This function only calculates a plan; the
    caller starts or stops processes after selection succeeds.

    Args:
        requirement (Requirement): Desired output, independent routes and budget.
        current (dict[str, Placement]): Current service assignments. Their
            processes count toward the replacement limit until they exit.
        settings (Settings): Search limit, process limits and minimum Cheeger value.

    Returns:
        Plan: The cheapest valid set of routes, preferring fewer new processes
            when costs are equal.

    Raises:
        RuntimeError: Too many route combinations need to be checked, or none
            satisfies the required result, cost, process and Cheeger limits.
    """
    paths: list[tuple[Placement, ...]] = []

    def extend(kind: str, path: tuple[Placement, ...]) -> None:
        """
        Extend a route with functions that can accept the preceding result.

        Args:
            kind (str): Label of the available value, such as raw or squared.
            path (tuple[Placement, ...]): Services selected so far, each used once.

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
    Run one assigned function using a worker pool that adjusts to its backlog.

    One service process owns this object and its child worker processes. Nature
    assigns the function, such as square(). Soul searching chooses between one
    interactive worker and three batch workers to execute that function.

    Each job carries the plan's version number, called its revision. The service
    rejects jobs for a different revision and remembers job IDs to reject
    duplicates. It can accept a new revision once all its jobs have finished.

    Attributes:
        placement (Placement): Fixed service name and function for this process.
        revision (int): Plan version that incoming jobs must match.
        pipe (Connection): Two-way pipe carrying parent commands and service reports.
        settings (soul.Settings): Worker adaptation settings and resource limits.
        children (dict[int, soul.Child]): Owned worker processes indexed by PID.
        pending (deque[tuple[int, int]]): Accepted jobs waiting for dispatch.
        inputs (dict[int, int]): Unfinished job IDs and their inputs to this service.
        accepted (set[int]): All job IDs received for this revision, including finished jobs.
        active (soul.Profile): Worker count and batch size currently receiving jobs.
        candidate (soul.Profile): Proposed profile, possibly still starting its workers.
        generation (int): Number of worker groups put into use by this service.
        high (int): Consecutive observations at or above the backlog threshold.
        completed (int): Checked results returned across all plan versions.
        previous (tuple[int, int]): Last reported backlog and completion counts.
        last_change (float): Monotonic time when the latest worker group took over.
        last_busy (float): Monotonic time of the most recent nonempty backlog.
        stopping (bool): Whether the parent has asked the service to stop accepting jobs.
        idle_sent (bool): Whether the parent was told this service is back to one
            worker with no unfinished jobs during the current quiet period.
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
        Prepare to run the assigned function with one interactive worker.

        Set up the job queue and worker tracking. The first run-loop iteration
        starts the worker; constructing this object creates no processes.

        Args:
            placement (Placement): Service name and function chosen by Nature.
            revision (int): Initial plan version expected on incoming jobs.
            pipe (Connection): Child endpoint of the parent's control channel.
            settings (Settings): Settings copied into the shared Soul searching code.
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
        Receive jobs, adjust workers to the backlog, and return checked results.

        Each pass reads messages, measures unfinished work, asks Soul searching
        for a worker profile, checks its limits and switches when new workers
        are ready. Old workers finish their accepted batches before exiting.
        This follows the same steps as soul.py's AdaptiveService.run().

        Returns:
            None: Stop was requested and every child worker has exited.

        Raises:
            RuntimeError: A command is invalid, a worker fails or a proposed
                worker profile exceeds a limit.
            OSError: Communication fails while exchanging jobs or status messages.
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
        Log an event with this service's name, function and plan version.

        Args:
            event (str): Decision, observation or worker-lifecycle event name.
            **details (Any): JSON-serializable measurements or explanations.

        Returns:
            None: The shared logger writes a JSON line to stdout.
        """
        soul.emit(event, service=self.placement.name, capability=self.placement.capability.name, revision=self.revision, **details)

    def admit_profile(self) -> None:
        """
        Start the proposed workers if process and Cheeger limits allow them.

        Count both old and new workers against the process limit. Existing
        workers keep receiving jobs while replacements start. If the proposed
        workers are already starting, another call leaves them in place.

        Returns:
            None: Proposed workers start, or there is no new group to start.

        Raises:
            RuntimeError: Too many workers would be alive, too many worker
                groups have been used, or the worker graph fails a Cheeger bound.
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
        Start one child worker to run the function assigned to this service.

        Args:
            profile (soul.Profile): Interactive or batch settings, including batch size.

        Returns:
            None: The child is recorded while waiting for its ready message.

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
        Send new jobs to the replacement group once all its workers are ready.

        Old workers finish their current batches and receive no further jobs.
        The first group to become ready also makes this service ready, which
        is reported to Nature before it sends any jobs.

        Args:
            now (float): Monotonic timestamp used for subsequent cooldown checks.

        Returns:
            None: The new group takes over, or the service keeps waiting for readiness.
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
        Read worker messages announcing readiness or returning a batch of results.

        Returns:
            None: Ready workers are marked and available results are checked and forwarded.

        Raises:
            RuntimeError: Returned job IDs or calculated results are incorrect.
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
        Check a worker's job IDs and answers, then forward the results to Nature.

        Recalculate each answer from the saved input as a correctness check for
        this demo. Include the current plan version in each result message.

        Args:
            child (soul.Child): Worker assigned this batch of jobs.
            results (list[tuple[int, int]]): Returned job IDs and calculated values.

        Returns:
            None: Verified results reach Nature and the worker's batch is cleared.

        Raises:
            RuntimeError: The returned IDs differ from the assigned batch or an
                answer differs from the assigned function's result.
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
        Remove exited workers after checking they finished their jobs and stopped cleanly.

        Returns:
            None: Finished workers are joined, their pipes closed and their records removed.

        Raises:
            RuntimeError: A worker exits without a stop request, with unfinished
                jobs or with a nonzero exit code.
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
        Read one parent command and check it is safe for the current service state.

        A job command carries a job ID, value and plan version. An adopt command
        asks the service to accept a new plan version after its work finishes.
        A stop command prevents new jobs while accepted jobs finish.

        Returns:
            None: Local state is updated and a plan change is acknowledged if requested.

        Raises:
            RuntimeError: A command is unknown, stale, duplicate or conflicts
                with unfinished work or a stop request.
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
            None: Idle workers receive a batch or a stop message as appropriate.

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
        Read parent commands until none are ready or 64 jobs are waiting for workers.

        Returns:
            None: Available jobs, revision changes and stop requests are processed.

        Raises:
            RuntimeError: A command is invalid, duplicated or refers to the wrong plan.
        """
        while self.pipe.poll() and len(self.pending) < 64:
            self.command()

    def observe(self, now: float) -> soul.Observation:
        """
        Measure unfinished jobs, recent progress and time since the last worker change.

        A snapshot records these values at this moment. Deltas are changes since
        the preceding observation: for example, completed_delta counts jobs
        finished since the last call. Soul searching uses this snapshot to
        decide whether sustained load or a quiet period warrants a worker change.

        Args:
            now (float): Current monotonic observation timestamp.

        Returns:
            soul.Observation: An immutable measurement used to choose a worker profile.
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
        Check that all workers are ready and none are still being replaced.

        Returns:
            bool: True when every owned worker is ready and none is retiring or stopping.
        """
        return bool(self.children) and all(child.ready and not child.retiring and not child.stopping for child in self.children.values())

    def propose_profile(self, observation: soul.Observation) -> None:
        """
        Choose between interactive and batch workers once the previous change finishes.

        Args:
            observation (soul.Observation): Current job counts and adaptation timing.

        Returns:
            None: Store the proposed profile, or keep the current proposal while
                a worker change or shutdown is in progress. admit_profile()
                separately checks the proposal's process and Cheeger limits.
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
        Tell Nature when work has finished and the service is back to one worker.

        Wait for the configured quiet interval and any worker replacement to
        finish. Send idle only once during each quiet period.

        Args:
            observation (soul.Observation): Unfinished job count and time without work.

        Returns:
            None: An idle message is sent when ready, or the service keeps waiting.
        """
        if observation.backlog or not self.workers_are_steady() or self.active != soul.INTERACTIVE:
            return
        if observation.quiet >= self.settings.idle_seconds and not self.idle_sent:
            self.pipe.send(("idle", self.revision, self.completed))
            self.idle_sent = True

    def finish_if_stopped(self) -> bool:
        """
        Report shutdown complete once every child worker has exited.

        Returns:
            bool: True after sending stopped, allowing the service loop to return.
        """
        if self.stopping and not self.children:
            self.pipe.send(("stopped", self.revision, self.completed))
            return True
        return False

    def close(self) -> None:
        """
        Stop remaining workers and close their pipes when the service exits.

        Request a normal stop first and wait for each worker to exit. Terminate
        workers that exceed the wait, then kill them if they still do not exit.
        This cleanup also runs after errors or interruption.

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
    Run a service in its own process and clean up its workers when it exits.

    multiprocessing calls this function when it starts a service. Leave Ctrl-C
    handling to the parent by ignoring SIGINT here. Convert SIGTERM into an
    exception so the finally block stops workers before this process exits.

    Args:
        placement (Placement): Service name and function assigned to this process.
        revision (int): Initial plan version expected on incoming jobs.
        pipe (Connection): Child endpoint for parent commands and observations.
        settings (Settings): Adaptation timing and process limits for the service.

    Returns:
        None: The service loop finished and its child workers were stopped and joined.

    Raises:
        RuntimeError: A command is invalid, a worker fails or a worker limit is exceeded.
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
    Track one running instance of a named service, including its process and pipe.

    Replacing A's square() function with fused() starts a new process. Both
    instances are called A, but each has a different process ID and Incarnation
    record. Nature retains these records after processes exit so final cleanup
    can account for every process it started.

    Attributes:
        placement (Placement): Service name and capability executed by this PID.
        process (BaseProcess): Spawned service process owned by Nature.
        pipe (Connection): Parent endpoint of the service control/data channel.
        flags (set[tuple[str, int]]): Received status labels paired with their plan
            version, such as (ready, 1) or (idle, 2).
    """

    placement: Placement
    process: BaseProcess
    pipe: Connection
    flags: set[tuple[str, int]] = field(default_factory=set)


@dataclass
class LoadRound:
    """
    Track the producer's jobs as they move through one round's service routes.

    Nature keeps each unfinished job's route and current processing step here.
    When B returns a squared value, Nature can use that record to send it to D
    for the next step. Each service separately tracks jobs assigned to its workers.

    Attributes:
        requirement (Requirement): Required final calculation: x*x or x*x + 1.
        plan (Plan): Ordered service routes selected for this round.
        generator (BaseProcess): Owned producer sending work to the parent.
        incoming (Connection): Read endpoint for producer work and end-of-input.
        total (int): Expected number of jobs from the producer.
        started (float): Monotonic start time for the round deadline and rate.
        inflight (dict[int, tuple[int, int]]): Each unfinished job's route and stage.
        completed (set[int]): Job IDs whose final results have been checked.
        accepted (int): Number of producer jobs sent to their first service so far.
        ending (bool): Whether the producer has sent None to mark the end of input.
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
    Choose services, connect their processing steps and supervise the experiment.

    This object lives in the parent process. It uses natural_selection() to
    choose a plan, starts the needed services and passes jobs between them.
    Each child service adjusts its own workers while running the function
    Nature assigned. Nature finishes a load round before changing the plan.

    Attributes:
        settings (Settings): Load settings, adaptation timing and process limits.
        context (mp.context.SpawnContext): multiprocessing factory that starts
            each producer and service in a fresh Python interpreter.
        active (dict[str, Incarnation]): Selected service processes, keyed by name.
        created (list[Incarnation]): All service processes retained for cleanup.
        generators (list[BaseProcess]): Producer processes owned by this parent.
        plan (Plan | None): Selected functions and service routes, initially absent.
        revision (int): Plan version, increased on each successful selection.
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
        Set up process tracking before any producer or service is started.

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
        Run all three scenarios, check their results, then shut down the services.

        Each scenario selects a plan and sends a finite load through it. Wait
        for every job to finish and every service to return to one interactive
        worker before applying the next scenario's requirements.

        Returns:
            int: Total verified producer jobs across every environment.

        Raises:
            RuntimeError: No valid plan is found, a result is wrong or a child fails.
            TimeoutError: Services take too long to become ready, accept a new
                plan version or shut down.
        """
        completed = 0
        for requirement in ENVIRONMENTS:
            plan = self.apply(requirement)
            completed += self.load(requirement, plan)
        self.retire_population()
        return completed

    def retire_population(self) -> None:
        """
        Stop all selected services after the last load round finishes.

        Returns:
            None: All services and their workers have exited and active is empty.

        Raises:
            RuntimeError: A service fails to stop cleanly.
            TimeoutError: A service takes too long to acknowledge shutdown.
        """
        for member in self.active.values():
            self.retire(member, "experiment complete")
        self.active.clear()

    def messages(self, members: list[Incarnation]) -> list[tuple[Incarnation, str, int, Any]]:
        """
        Read available service messages and check for unexpected process exits.

        Read at most 128 messages per service in the order received. Remember
        status messages such as ready and adopted, together with their plan
        version, so barrier() can wait for all services to reach the same state.
        Return result messages with their job IDs and values for routing.

        Args:
            members (list[Incarnation]): Live services whose pipes may be read.

        Returns:
            list[tuple[Incarnation, str, int, Any]]: Source service record, message
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
        Wait until every listed service confirms a state for the given plan version.

        For example, wait for ready before using new services, or adopted before
        sending jobs with a new plan version. Calls happen between load rounds,
        when all jobs should be complete. An empty list returns immediately.

        Args:
            members (list[Incarnation]): Services that must acknowledge the state.
            kind (str): Status message to wait for, such as ready or adopted.
            revision (int): Plan revision to which each acknowledgement belongs.

        Returns:
            None: All members have acknowledged the requested state and revision.

        Raises:
            RuntimeError: A job result arrives when work should already be
                finished, or a service fails.
            TimeoutError: Services do not all respond within settings.timeout seconds.
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
        Choose the next plan, ready its services, switch routes and stop old services.

        Call this after the previous round's jobs have finished. Keep services
        whose assignments still fit, start replacements and wait for readiness.
        Every selected service must accept the new plan version before routing
        switches. Services excluded from the new plan can then shut down.

        Args:
            requirement (Requirement): Outcome and budget for the new environment.

        Returns:
            Plan: The active routes and assignments, with old services stopped.

        Raises:
            RuntimeError: No valid plan is found or a service fails a required check.
            TimeoutError: A service takes too long to become ready, accept the
                plan version or shut down.
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
        Choose a valid plan, give it a new version number and log the decision.

        Args:
            requirement (Requirement): Required output, independent routes and cost ceiling.

        Returns:
            Plan: Routes chosen by natural_selection(), ready for process setup.

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
        Reuse unchanged services and start processes for new or changed assignments.

        Args:
            plan (Plan): Selected assignments whose process limits have already
                been checked, including old and replacement processes alive together.

        Returns:
            tuple[dict[str, Incarnation], list[Incarnation]]: All services in the
                next plan, keyed by name, followed by the newly started services
                whose ready messages the caller must await.
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
        Start a service process with its assigned function and keep its cleanup record.

        Args:
            placement (Placement): Service name and capability for the new process.
            previous (Incarnation | None): Process being replaced, if this service
                is changing its function. Used to identify the change in logs.

        Returns:
            Incarnation: The new process and the parent's end of its pipe. The
                caller must still wait for the service's ready message.

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
        Ask every selected service to accept jobs with the new plan version.

        Wait for all replies before the parent sends jobs. A service rejects
        later job messages if their version differs from the one accepted here.

        Args:
            members (list[Incarnation]): Ready service processes chosen for the next plan.

        Returns:
            None: Every service has acknowledged the new version with no jobs unfinished.

        Raises:
            RuntimeError: A service rejects the version change or returns an
                unexpected job result while the parent is waiting.
            TimeoutError: Services do not all acknowledge the version within the timeout.
        """
        for member in members:
            member.pipe.send(("adopt", self.revision, None))
        self.barrier(members, "adopted", self.revision)

    def commit_plan(self, plan: Plan, next_members: dict[str, Incarnation]) -> list[Incarnation]:
        """
        Switch to the ready services and log which job-routing connections changed.

        Args:
            plan (Plan): Routes whose services have accepted the current plan version.
            next_members (dict[str, Incarnation]): Ready service assignments keyed by name.

        Returns:
            list[Incarnation]: Old service processes that the caller must now stop.
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
        Stop services left out of the new plan and log why each was replaced or removed.

        Args:
            members (list[Incarnation]): Excluded old service processes.

        Returns:
            None: Each excluded service has exited with an explicit selection reason.

        Raises:
            RuntimeError: A service fails to stop cleanly.
            TimeoutError: A service takes too long to acknowledge shutdown.
        """
        for member in members:
            reason = "capability replaced" if member.placement.name in self.active else "excluded by outcome and cost budget"
            self.retire(member, reason)

    def retire(self, member: Incarnation, reason: str) -> None:
        """
        Ask a service to stop its workers, then wait for the service itself to exit.

        Call after a load round finishes. The service stops accepting jobs,
        shuts down its workers and sends stopped. Wait for its process to exit
        successfully, close the pipe and log the reason for removing it.

        Args:
            member (Incarnation): Service process to stop.
            reason (str): Human-readable selection or shutdown explanation.

        Returns:
            None: The service process exited successfully and its pipe is closed.

        Raises:
            RuntimeError: A job result arrives when work should already be
                finished, or the service fails to exit successfully.
            TimeoutError: The service does not acknowledge shutdown within the timeout.
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
        Send one round of jobs through the services and check their answers and recovery.

        Args:
            requirement (Requirement): Required calculation used to check final answers.
            plan (Plan): Active service functions and routes.

        Returns:
            int: Number of completed producer jobs, after all services return to one worker.

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
        Start a producer with enough jobs to exercise every selected route.

        Clear earlier idle and batch reports so this round must demonstrate
        its own change to batch workers and return to one interactive worker.

        Args:
            requirement (Requirement): Required output for the round.
            plan (Plan): Routes that will receive jobs in turn.

        Returns:
            LoadRound: Producer process, receiving pipe and empty job-tracking records.

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
        Assign new jobs to routes while the unfinished-job limit has room.

        Rotate through the routes so each receives the same number of jobs.
        At the limit, stop reading from the producer until some jobs finish.

        Args:
            round_state (LoadRound): Producer pipe, selected routes and unfinished jobs.

        Returns:
            None: Jobs are sent to their first service, or input waits for capacity.

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
        Send a job to one service, marking it busy and including the plan version.

        Args:
            member (Incarnation): Selected service responsible for this processing step.
            identity (int): Job ID kept unchanged throughout its route.
            value (int): Original input or checked output from the preceding service.

        Returns:
            None: The job is sent and the service's earlier idle flag is cleared.
        """
        member.flags.discard(("idle", self.revision))
        member.pipe.send(("job", self.revision, (identity, value)))

    def route_results(self, round_state: LoadRound) -> None:
        """
        Read service messages and pass each result to its next processing step.

        Args:
            round_state (LoadRound): Each unfinished job's position and finished job IDs.

        Returns:
            None: Available results have advanced or completed their jobs.

        Raises:
            RuntimeError: A message belongs to a different plan version or a result is invalid.
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
        Check which service answered, then pass on its result or verify the final answer.

        For the B -> D route, send B's result to D. When D answers, compare it
        with the original input squared plus one and mark the job complete.

        Args:
            round_state (LoadRound): Routes, required calculation and job-tracking records.
            member (Incarnation): Service that returned the result.
            identity (int): Original job ID carried by the result.
            value (int): Computed output of this stage.

        Returns:
            None: The next service receives the job, or completion frees room for a new job.

        Raises:
            RuntimeError: The wrong service answered, the final value is wrong
                or that job has already been marked complete.
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
        Check that all input is processed and every service is idle with one worker.

        Args:
            round_state (LoadRound): Producer completion flag and unfinished jobs.

        Returns:
            bool: True when input has ended, no jobs remain and all services report idle.
        """
        return (
            round_state.ending
            and not round_state.inflight
            and all(("idle", self.revision) in member.flags for member in self.active.values())
        )

    def check_round_health(self, round_state: LoadRound) -> None:
        """
        Check that the producer has not failed and the round has not timed out.

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
        Confirm every job finished once and every service used batch workers this round.

        load() calls this after all services have returned to one interactive
        worker. Log the number of completed jobs, elapsed time, completion rate
        and service process IDs so the round can be inspected afterward.

        Args:
            round_state (LoadRound): Finished round with completed job IDs recorded.

        Returns:
            int: Exact number of verified producer jobs in this environment.

        Raises:
            RuntimeError: The producer did not exit successfully, completed job
                IDs differ from those expected, or a service never reported batch mode.
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
        Stop remaining producers and services after the demo finishes or fails.

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
    Read command-line options and check their ranges before starting any processes.

    Returns:
        Settings: Checked job counts, timing values and process limits for the demo.

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
    Configure the demo, run all three scenarios and clean up every child process.

    Read Nature.run() next for scenario order, Nature.apply() for service
    replacement, Nature.load() for job routing and Service.run() for worker
    adaptation inside each service.

    Returns:
        None: Every scenario finished, cleanup ran and the success event was logged.

    Raises:
        SystemExit: Command-line help or validation ends the invocation.
        RuntimeError: No valid plan is found, a job check fails or a child fails.
        TimeoutError: A wait for service readiness, plan acknowledgement or shutdown expires.
        KeyboardInterrupt: Cancellation runs cleanup and then ends the demo.
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
