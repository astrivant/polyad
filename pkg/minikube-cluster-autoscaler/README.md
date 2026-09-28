# Native Minikube autoscaler provider

<!-- toc:start -->
**Table of contents**

- [Native Minikube autoscaler provider](#native-minikube-autoscaler-provider)
<!-- toc:end -->

Optional standalone Go module implementing Kubernetes Cluster Autoscaler's
`externalgrpc` protocol plus a narrowly scoped native Minikube bridge. It is not
installed by the Python operator or SDK packages.

See [activation, constraints and recovery](../../integrations/minikube/autoscaler/README.md).

```bash
go -C pkg/minikube-cluster-autoscaler test -race ./...
go -C pkg/minikube-cluster-autoscaler vet ./...
```

`state.go` journals desired workers before mutations. `bridge.go` independently
enforces base ownership and the memory ceiling outside the container's writable
state. `backend.go` invokes scoped native commands and checks safe deletion.
`rpc.go` returns cached group state so slow VM provisioning cannot block gRPC.
`tls.go` separates autoscaler-client and bridge-client identities.

The tests use fake VM operations, real protobuf RPCs, and local TLS listeners.
Live node growth/shrinkage is an explicit integration test, never part of unit CI.

The files in `internal/protos/` are copied unchanged from Kubernetes Autoscaler
tag `cluster-autoscaler-1.35.0`, path `cluster-autoscaler/cloudprovider/externalgrpc/protos/`.
Their Kubernetes Authors copyright and Apache-2.0 license are retained; see
[LICENSE.kubernetes](LICENSE.kubernetes). Regenerate only against the pinned
upstream protocol and verify the Node protobuf compatibility test.
