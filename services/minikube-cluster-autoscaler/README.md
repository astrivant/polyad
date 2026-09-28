# Host autoscaler container

<!-- toc:start -->
**Table of contents**

- [Host autoscaler container](#host-autoscaler-container)
<!-- toc:end -->

Build and run through the [Minikube addon](../../integrations/minikube/autoscaler/README.md).
The multi-stage Dockerfile builds only the optional Go module into a scratch image.
Runtime is non-root, read-only, capability-free and bounded to 256 MiB / 0.5 CPU
by the activation script. Only provider state and client/server TLS identities
are bind-mounted. Native VM commands execute in the separately authenticated
host bridge, not inside Docker Desktop's Linux VM.

There is deliberately no Docker socket or host kubeconfig mount. Stopping the
container stops new scaling decisions; it does not delete existing workers.
