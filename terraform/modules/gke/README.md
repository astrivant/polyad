# GKE test cluster module

Called by the [Terraform root module](../../README.md); it creates one zonal GKE
Standard cluster, an untainted `default` system pool, and a dedicated `polyad`
pool with **2–10 total nodes**. Both pools use `UBUNTU_CONTAINERD`.

## Table of contents

- [Inputs and outputs](#inputs-and-outputs)
- [Capacity and identity](#capacity-and-identity)

## Inputs and outputs

| Input | Type | Meaning |
| --- | --- | --- |
| `project_id` | string | Existing billing-enabled Google Cloud project |
| `region` | string | Subnet region |
| `zone` | string | Control-plane and node zone in that region |
| `name` | string | Cluster/network prefix, at most 24 characters |
| `machine_type` | string | Polyad node shape; default `c3-standard-4` |
| `default_node_count` | number (integer) | Fixed untainted `e2-highcpu-2` system nodes; default `1` |
| `deletion_protection` | bool | Prevent cluster deletion; default `false` |

Outputs are the cluster `name`, HTTPS `endpoint` (without scheme), and base64
`ca_certificate`. They depend on node pool creation so consumers can wait for
schedulable capacity before installing Helm releases.

## Capacity and identity

GKE's temporary creation pool is removed and replaced by a Terraform-managed
system pool named `default`, plus the experiment pool named `polyad`. The default
pool has one untainted node unless configured otherwise. Polyad starts at two;
the cluster autoscaler owns subsequent counts within the total 2–10 bounds. Its
`dedicated=polyad:NoSchedule` taint and workload node selectors separate project
services from ordinary system Pods. GKE's per-node agents can still run on the
experiment pool. Node auto-provisioning is disabled.
Regular-channel upgrades and repairs remain enabled; Polyad pool upgrades have zero surge
and one unavailable node, so usable capacity can temporarily fall below two.
These are autoscaler bounds, not a Google Cloud billing quota.

The module creates its own VPC and subnet, with node range `10.10.0.0/20`, Pod
range `10.20.0.0/16` and Service range `10.30.0.0/20`. It uses public node IPs,
a public authenticated control-plane endpoint, Dataplane V2 and Workload Identity.
Its node service account has `roles/container.defaultNodeServiceAccount` rather
than the project's default Compute Engine identity. No service-account keys are
created. This module is intended for the isolated test environment described in
the root guide; network peering, private endpoints and production hardening are
separate deployment choices.
