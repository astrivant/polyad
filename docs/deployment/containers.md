# Development and production containers

Use Docker Buildx to build either runnable target. A build without `--target`
selects `production`. Both targets support `linux/amd64` and `linux/arm64`.

See the [process and thread hierarchy](process-hierarchy.md) for runtime diagrams,
async task ownership and shutdown order.

| Target | Contents | Intended use |
| --- | --- | --- |
| `production` | Installed Polyad wheel and runtime dependencies from `poetry.lock` | Kubernetes operator deployments |
| `development` | Editable source, locked development dependencies, Poetry and Git | Python development, debugging and unit tests |

## Table of contents

- [Production](#production)
- [Runtime capabilities](#runtime-capabilities)
- [Development](#development)
- [Health and build checks](#health-and-build-checks)
  - [Image metadata](#image-metadata)
  - [Runtime checks](#runtime-checks)

## Production

Create a Buildx builder once, or select an existing one that supports the desired
platforms. Docker Desktop includes emulation for building another architecture;
on standalone Linux, use native builder nodes or configure QEMU first. See
[Docker's multi-platform build guide](https://docs.docker.com/build/building/multi-platform/).

```sh
docker buildx create --name polyad --driver docker-container --use
docker buildx inspect --bootstrap
docker buildx build --target production \
    --platform linux/amd64,linux/arm64 \
    --tag YOUR_REGISTRY/polyad:VERSION --push .
```

Log in to your registry before using `--push`. This command publishes both
architectures under one tag, so Kubernetes can pull the image matching each node.
For local testing, build one platform and use `--load` to make the image available
to `docker run` or `kind load docker-image`:

```sh
docker buildx build --target production --load --tag polyad:production .
```

Without `--platform`, the builder uses its default platform. Use an explicit
`--platform linux/amd64` or `--platform linux/arm64` when targeting a different
machine. Choose `--push` for the combined multi-platform image and `--load` for a
single local image; a container-backed builder otherwise leaves its result only
in the build cache.

The production stage copies only the installed virtual environment from the
builder. Poetry, tests, source checkout and development packages stay out of the
final image. The Python/Debian base and Dockerfile frontend are pinned by version
and digest. Poetry's version comes from `.tool-versions`; Tini and development-only
Git (including `git-man`) use exact Debian package versions in the Dockerfile.
The build validates the committed `poetry.lock` and fails if it is stale, rather
than regenerating it inside the image. Update the lockfile before rebuilding after
dependency changes.

Refresh pins deliberately when adopting Python or OS updates.
`--build-arg PYTHON_IMAGE=...`, `--build-arg TINI_VERSION=...` and
`--build-arg GIT_VERSION=...` allow explicitly selected replacements. Debian
repositories must still provide those versions; these pins do not freeze their
transitive OS dependencies or Poetry's own bootstrap dependencies.

Every stage runs for the target platform, including dependency installation.
This keeps native Python extensions compatible with the final image. Pip and
Poetry caches are separated by target architecture and locked during use, so
concurrent builds do not share incompatible cached artifacts.

Both images install Debian's [Tini](https://github.com/krallin/tini) package in the
shared base. `/usr/bin/tini -- python -m polyad.operator.runtime` runs Tini as
PID 1 to reap orphaned children and forward signals to Python. Both processes
run as UID/GID `65532`; PID 1 does not require root privileges. Python retains its
SIGTERM, SIGINT and SIGHUP handlers and runs Kopf on its dedicated thread.
Docker's stop signal is SIGTERM; allow enough time for queued writes, listeners
and trace export to drain before forcing exit. Logs remain unbuffered and
bytecode writes disabled.

Helm explicitly preserves this command for dense operators, split components
and bootstrap deployments. Observer containers use
`/usr/bin/tini -- python -m polyad.operator.observer`. Root-managed Deployment
and DaemonSet workers inherit the root's container command. Custom operator images
must provide `/usr/bin/tini` as well as the Python runtime.

The [Helm deployment](../introduction/getting-started.md#quick-start-kubernetes) provides a read-only
root filesystem, dropped capabilities, no privilege escalation, service-account
credentials, shared-cache connectivity and a termination grace period. The image
supports those settings. Standalone Docker use still needs a reachable Kubernetes
API and Redis/Dragonfly endpoint; the development target does not emulate them.

## Runtime capabilities

Both image targets install all runtime extras, including `flask-auth`, from the
lockfile. Helm selects capabilities at startup; enabling a supported feature does
not require rebuilding the image or installing packages in a running Pod. The
Python distribution retains optional extras for users building their own runtime.

The chart passes feature values as environment variables. Python checks them
before importing optional implementations. Split components also check
`POLYAD_COMPONENT`, so an executor does not load HTTP listeners enabled for the
gateway or telemetry components.

| Chart configuration | Runtime import behavior |
| --- | --- |
| `api.enabled`, `events.enabled`, `connections.enabled`, `metrics.enabled` | Load the HTTP transport only if this component serves a listener; import each endpoint builder only when registering that family. |
| `events.websockets.enabled` with `events.enabled` | Import Hypercorn only when starting a component that serves WebSocket events; it replaces Waitress for all that process's listeners and retains the existing Flask application. The image includes `websockets` and Hypercorn dependencies. |
| `events.enabled` or `postgresql.events.enabled` with PostgreSQL enabled | Enable executor event publication through `POLYAD_EVENT_PUBLICATION_ENABLED`, independently of which component serves streams. |
| `postgresql.enabled` | Load the state store and PostgreSQL drivers when enabled. |
| `postgresql.recordEncryption.enabled` | Database writers import the record cipher and `cryptography` only when enabled. Existing Secret references become read-only key files; the dependency is included in the production image. See [record encryption](record-encryption.md). |
| `authentication.storage.enabled` | The projected database credential enables the authentication store when named keys are configured. State storage can remain disabled. |
| `authentication.backend: FlaskHTTPAuth` | Import Flask-HTTPAuth when installing authentication for a protected endpoint. `Builtin` and demonstration mode leave it unloaded. |
| `api.rateLimit.enabled` | Import Flask-Limiter when installing an enabled quota. Demonstration mode skips it. Named-key lanes retain their separate limits. |
| `tracing.enabled` | Import the OpenTelemetry SDK and OTLP exporter only when enabled and `OTEL_SDK_DISABLED` is not true. The lightweight tracing API remains available to shared instrumentation. |
| `rootControlPlane.enabled` | Load root coordination and remote worker management only in root mode. Remote workers retain the upstream observation duties required by that mode. |
| `dragonfly.ha.enabled` and `dragonfly.autoscaling.enabled` with bundled Dragonfly enabled | Load the DragonflyPool controller for planning or its metrics collector when required. |

Core graph validation, networking enforcement, Kubernetes access and shared queue
coordination remain available to execution workers. Disabling an HTTP endpoint
does not disable reconciliation of existing resources or their cleanup. Helm and
KEDA controllers run separately; installing their chart resources does not imply
a matching Python dependency. Changing capability values replaces Pods; imports
are not dynamically unloaded within a running process.

## Development

```sh
docker buildx build --target development --load -t polyad:dev .
docker run --rm -it --entrypoint /usr/bin/tini \
    --mount type=bind,source="$PWD/pkg",target=/app/pkg \
    polyad:dev -- /bin/sh
```

The package is installed in editable mode, so edits under the mounted `pkg`
directory are used by subsequent Python processes. Restart a running operator to
load changed modules. The default development log level is DEBUG; it can be
overridden with `POLYAD_LOG_LEVEL` or `--log-level`. Use `--debug` as a shorthand
for `--log-level DEBUG` on either the operator or observer entrypoint.

Run Python tests under the same init process:

```sh
docker run --rm --entrypoint /usr/bin/tini polyad:dev \
    -- python -m pytest -n 2 pkg/tests/test_balance_gates.py
```

Both targets use the same non-root identity. The development image owns `/app`,
its virtual environment and its home directory; host bind-mount permissions still
apply. Helm, Node, Argo CD, Go and a Kubernetes cluster are separate integration-test
tools, as described in the [toolchain guide](../development/toolchain.md).

## Health and build checks

### Image metadata

Both profiles carry [OCI image labels](https://github.com/opencontainers/image-spec/blob/main/annotations.md)
for the title, description, authors, vendor, project URL, documentation, source and
SPDX license. `com.astrivant.polyad.profile` identifies the build target.

CI also records the package version, checked-out Git revision and RFC 3339 build
timestamp. Supply the same metadata when building images locally:

```sh
docker buildx build --target production --load \
    --build-arg VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')" \
    --build-arg VCS_REF="$(git rev-parse HEAD)" \
    --label "org.opencontainers.image.created=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    -t polyad:production .
```

Without those options, version and revision are empty and the build timestamp is
omitted. These are image configuration labels; registry manifest and index
annotations are separate publishing metadata.

### Runtime checks

The image health check calls the existing Kopf `/healthz` endpoint on port 8080.
It includes a startup allowance and fails when the runtime requests replacement or
starts draining. Kubernetes uses the chart's startup, readiness and liveness probes
instead of Docker's image health check. If overriding `--liveness` for standalone
Docker use, override the Docker health-check command to match.

Ports 8080, 8090, 8091 and 8092 are declared for health, composition, events and
metrics. Declaring them does not enable the optional APIs or publish host ports.

CI uses Buildx to build and load both targets on native AMD64 and ARM64 runners,
with separate GitHub Actions layer caches for each profile and architecture.
The build action uses the checked-out working directory (`context: .`), including
release metadata and lockfile changes prepared earlier in the job. The release
preparation step refreshes the lockfile after changing package versions, before
the Dockerfile validates it. These CI jobs test images locally; they do not
publish them to a registry.

CI verifies Tini is PID 1 and forwards SIGTERM, checks imports
with a read-only filesystem and dropped
capabilities, verifies production excludes development tools, and runs development
tests. The Kind integration also uses Buildx with a single-platform load before
importing the production image into the test cluster. `.dockerignore`
excludes local virtual environments, caches, build output and common credential
files from the build context; provide credentials at runtime through mounts or
Kubernetes Secrets.
