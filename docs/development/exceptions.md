# Categorized Python exceptions

<!-- toc:start -->
**Table of contents**

- [Import categories](#import-categories)
- [Compatibility and dependencies](#compatibility-and-dependencies)
- [Adding an exception](#adding-an-exception)
<!-- toc:end -->

Every Python distribution has an `exceptions/` package under its importable
package directory. Custom exceptions are **defined only there**. Implementations
and applications import the relevant category; they do not need to import an
HTTP transport, controller or graph computation just to catch its failures.

## Import categories

| Import module | Defined exceptions |
| --- | --- |
| `polyad.exceptions.api` | `RequestError`, `Conflict`, `Unauthorized`, `Forbidden`, `Unavailable` |
| `polyad.exceptions.auth` | `LaneFull` |
| `polyad.exceptions.compiler` | `PreconditionFailed` |
| `polyad.exceptions.coordination` | `NotOwner`, `PulseDeferred` |
| `polyad.exceptions.events` | `CursorExpired`, `TopologyReplaced` |
| `polyad.exceptions.graph` | `CheegerIncomplete` |
| `polyad.exceptions.kubernetes` | `WriteConflict` |
| `polyad.exceptions.policies` | `PolicyViolation` |
| `polyad.exceptions.reconciliation` | `Pending` |
| `polyad_sdk.exceptions.api` | `APIError` |
| `polyad_sdk.exceptions.events` | `StreamInterrupted` |
| `polyad_types.exceptions.events` | `EventTooLarge` |

Each package's `exceptions` root also re-exports its public exception classes:

```python
from polyad_sdk.exceptions.api import APIError
from polyad_sdk.exceptions.events import StreamInterrupted
from polyad_types.exceptions.events import EventTooLarge

# A convenient alternative when importing several categories:
from polyad.exceptions import Pending, PolicyViolation
```

`polyad_sdk.exceptions.processes` contains the supervisor's private `_Aborted`
control-flow signal. It remains outside `__all__`; applications use `PlanResult`
to observe process-plan outcomes instead of catching this internal signal.
`polyad_schemas.exceptions` and `polyad_benchmarks.exceptions` are intentionally
empty: those packages currently define no custom exceptions.

## Compatibility and dependencies

Existing public imports, such as `polyad_sdk.APIError`,
`polyad_sdk.events.StreamInterrupted`, `polyad_types.EventTooLarge` and
`polyad.api.http.errors.Conflict`, remain aliases of the canonical classes.
Previously defining modules also retain their exception aliases. These are
not wrapper subclasses: existing `except` clauses catch the same objects.
Class `__module__` metadata now points to the defining exception category.

Inheritance, messages and diagnostic attributes are unchanged. In particular,
`Conflict` remains both a `RequestError` and a `ValueError`, `WriteConflict`
remains a Kubernetes `ApiException`, and retry delays, lifecycle phases,
API response bodies and Cheeger certificates retain their existing semantics.
No new common base class changes how broad exception handlers behave.

Only requesting `WriteConflict` loads its Kubernetes base class. Importing the
operator's other exception categories does not initialize numerical engines,
controllers, Redis or PostgreSQL clients. Wildcard-importing the operator's
exception root requests every public class, including `WriteConflict`.
SDK categories do not load optional protocol or reachability dependencies.

Built-in and third-party exceptions are **not** re-exported. Import `ValueError`
from Python's built-ins and dependency-specific exceptions from the library
that defines them. Their behavior and existing raise sites are not changed.

## Adding an exception

Define it once in the owning package's appropriate `exceptions/<category>.py`,
with typed signatures, Google-style docstrings and a static `__all__` entry if
public. Re-export public classes from that package's exception root and import
the category directly in consumers. Do not duplicate a shared type in the SDK
or operator: shared event validation failures belong to `polyad_types`.

Use type-checking-only imports for payload annotations that would otherwise
import the exception's consumer. Tests check central definition ownership,
legacy alias identity, payload behavior and dependency isolation. The existing
public-export pre-commit check also covers every exception module.
