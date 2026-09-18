# Service images

The two benchmark images share the standalone `polyad-benchmarks` package and its Poetry lock.
The operator image is built from `services/operator/Dockerfile`. All builds use the repository root as their context.

## Table of contents

- [Images](#images)
- [Build](#build)

## Images

| Dockerfile | Entry point | Role |
| --- | --- | --- |
| [operator/Dockerfile](operator/Dockerfile) | `polyad.operator.runtime` | Operator production and development targets |
| [runner/Dockerfile](runner/Dockerfile) | `polyad_benchmarks.runner` | Bounded arrivals and activation completion measurements |
| [fixture/Dockerfile](fixture/Dockerfile) | `polyad_benchmarks.fixture` | Mock service translating arrivals into pulses; `--once` performs a batch |

The benchmark images use a digest-pinned Python base, pinned Tini, locked Python dependencies,
a non-root user, and target-platform builds. The fixture listener binds the
Downward API Pod IP, falling back to loopback for local checks. Images expose no
public Service. Configure a separate fixture token in shared test environments.

## Build

Run from the repository root, replacing the registry and tag with your own:

```sh
docker buildx build --platform linux/amd64,linux/arm64 --target production \
  -f services/operator/Dockerfile -t YOUR_REGISTRY/polyad:RUN --push .
docker buildx build --platform linux/amd64,linux/arm64 \
  -f services/runner/Dockerfile -t YOUR_REGISTRY/polyad-benchmarks-runner:RUN --push .
docker buildx build --platform linux/amd64,linux/arm64 \
  -f services/fixture/Dockerfile -t YOUR_REGISTRY/polyad-benchmarks-fixture:RUN --push .
```

Pass both repositories and their shared tag in the [benchmark chart](../charts/polyad-benchmarks/README.md).
The CI workflow builds and smoke-checks both architectures; it does not publish
images or deploy a cloud test. See [the load study](../studies/load/README.md).
