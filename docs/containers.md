# Development and production containers

The Dockerfile has two runnable targets. A build without `--target` selects
`production`.

| Target | Contents | Intended use |
| --- | --- | --- |
| `production` | Installed Polyad wheel and runtime dependencies from `poetry.lock` | Kubernetes operator deployments |
| `development` | Editable source, locked development dependencies, Poetry and Git | Python development, debugging and unit tests |

## Production

```sh
docker build --target production -t YOUR_REGISTRY/polyad:VERSION .
docker push YOUR_REGISTRY/polyad:VERSION
```

The production stage copies only the installed virtual environment from the
builder. Poetry, tests, source checkout and development packages stay out of the
final image. The Python/Debian base is pinned by tag and digest, while Poetry's
version comes from `.tool-versions`. Refresh the base pin when adopting Python or
OS updates; `--build-arg PYTHON_IMAGE=...` allows an explicitly selected replacement.

The process runs as UID/GID `65532`, emits unbuffered logs, disables bytecode writes
and starts `python -m polyad.operator.runtime` directly. Python owns SIGTERM and
SIGINT and starts Kopf on its dedicated thread. Docker's stop signal is SIGTERM;
allow enough time for queued writes and listeners to drain before forcing exit.

The [Helm deployment](getting-started.md#quick-start-kubernetes) provides a read-only
root filesystem, dropped capabilities, no privilege escalation, service-account
credentials, shared-cache connectivity and a termination grace period. The image
supports those settings. Standalone Docker use still needs a reachable Kubernetes
API and Redis/Dragonfly endpoint; the development target does not emulate them.

## Development

```sh
docker build --target development -t polyad:dev .
docker run --rm -it --entrypoint /bin/sh \
    --mount type=bind,source="$PWD/pkg",target=/app/pkg \
    polyad:dev
```

The package is installed in editable mode, so edits under the mounted `pkg`
directory are used by subsequent Python processes. Restart a running operator to
load changed modules. The default development log level is DEBUG; it can be
overridden with `POLYAD_LOG_LEVEL` or `--log-level`.

Run Python tests without changing the operator entrypoint:

```sh
docker run --rm --entrypoint python polyad:dev \
    -m pytest -n 2 tests/test_balance_gates.py
```

Both targets use the same non-root identity. The development image owns `/app`,
its virtual environment and its home directory; host bind-mount permissions still
apply. Helm, Node, Argo CD, Go and a Kubernetes cluster are separate integration-test
tools, as described in the [toolchain guide](toolchain.md).

## Health and build checks

### Image metadata

Both profiles carry [OCI image labels](https://github.com/opencontainers/image-spec/blob/main/annotations.md)
for the title, description, authors, vendor, project URL, documentation, source and
SPDX license. `com.astrivant.polyad.profile` identifies the build target.

CI also records the package version, checked-out Git revision and RFC 3339 build
timestamp. Supply the same metadata when building images locally:

```sh
docker build --target production \
    --build-arg VERSION="$(python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["tool"]["poetry"]["version"])')" \
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

CI builds both targets, checks imports with a read-only filesystem and dropped
capabilities, verifies production excludes development tools, and runs development
tests. The existing Kind integration deploys the production target. `.dockerignore`
excludes local virtual environments, caches, build output and common credential
files from the build context; provide credentials at runtime through mounts or
Kubernetes Secrets.
