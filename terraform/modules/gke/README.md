# GKE test cluster module

<!-- toc:start -->
**Table of contents**

- [Inputs and outputs](#inputs-and-outputs)
- [Capacity and identity](#capacity-and-identity)
<!-- toc:end -->

Called by the [Terraform root module](../../README.md); it creates one zonal GKE
Standard cluster and four pools: untainted `default`, `polyad` for the operator,
`fixtures` for consumers and supporting services, and `copolyad` for load
generators. All pools use `UBUNTU_CONTAINERD`.

## Inputs and outputs

| Input | Type | Meaning |
| --- | --- | --- |
| `project_id` | string | Existing billing-enabled Google Cloud project |
| `region` | string | Subnet region |
| `zone` | string | Control-plane and node zone in that region |
| `name` | string | Cluster/network prefix, at most 24 characters |
| `machine_type` | string | Shared `polyad`, `fixtures` and `copolyad` node shape; default `c3-standard-4` |
| `default_node_count` | number (integer) | Fixed untainted `e2-highcpu-2` system nodes; default `1` |
| `fixtures_min_nodes`, `fixtures_max_nodes` | number (integer) | Support/consumer pool bounds; defaults `1`, `10` |
| `copolyad_min_nodes`, `copolyad_max_nodes` | number (integer) | Generator pool bounds; defaults `1`, `10` |
| `deletion_protection` | bool | Prevent cluster deletion; default `false` |

Outputs are the cluster `name`, HTTPS `endpoint` (without scheme), and base64
`ca_certificate`. They depend on node pool creation so consumers can wait for
schedulable capacity before installing Helm releases.

## Capacity and identity

GKE's temporary creation pool is removed and replaced by a Terraform-managed
system pool named `default`, plus `polyad`, `fixtures` and `copolyad`. The default
pool has one untainted node unless configured otherwise. Polyad starts at two;
the cluster autoscaler owns subsequent counts within its total 2–10 bounds.
Fixtures and copolyad each start at their configured minimum (default one) and
independently scale up to their maximum (default ten). Minimums must be positive
integers and maximums at least their corresponding minimums.

The three experiment pools share `machine_type`. Each has a
`dedicated=<pool>:NoSchedule` taint and requires matching workload selectors and
tolerations. This separates producers from consumers and from the operator.
GKE's necessary per-node agents can still tolerate these taints. Node
auto-provisioning is disabled. Regular-channel upgrades and repairs remain
enabled; experiment pool upgrades use zero surge and one unavailable node, so
usable capacity can temporarily fall below the minimum. Initial counts are
ignored on subsequent applies to preserve the autoscaler's live counts.

The module creates its own VPC and subnet, with node range `10.10.0.0/20`, Pod
range `10.20.0.0/16` and Service range `10.30.0.0/20`. It uses public node IPs,
a public authenticated control-plane endpoint, Dataplane V2 and Workload Identity.
Its node service account has `roles/container.defaultNodeServiceAccount` rather
than the project's default Compute Engine identity. No service-account keys are
created. This module is intended for the isolated test environment described in
the root guide; network peering, private endpoints and production hardening are
separate deployment choices.
