variable "project_id" {
  description = "Existing, billing-enabled Google Cloud project for this isolated test environment."
  type        = string
}

variable "region" {
  description = "Google Cloud region containing the cluster zone."
  type        = string
  default     = "us-central1"
}

variable "zone" {
  description = "One zone for the test cluster and its four node pools. This is a zonal, not a regional HA, control plane."
  type        = string
  default     = "us-central1-a"

  validation {
    condition     = startswith(var.zone, "${var.region}-")
    error_message = "zone must belong to region."
  }
}

variable "cluster_name" {
  description = "Name of the dedicated GKE cluster and prefix for its network and node identity."
  type        = string
  default     = "polyad-load-test"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,22}[a-z0-9]$", var.cluster_name))
    error_message = "Use 2–24 lowercase letters, digits or hyphens, starting with a letter and ending with a letter or digit."
  }
}

variable "machine_type" {
  description = "Shared machine type for polyad, fixtures and copolyad; holds node size constant while isolating producers, consumers and the operator."
  type        = string
  default     = "c3-standard-4"
}

variable "default_node_count" {
  description = "Fixed untainted e2-highcpu-2 nodes for GKE services; separate from Polyad's autoscaling 2–10 nodes."
  type        = number
  default     = 1
}

variable "deletion_protection" {
  description = "Protect the cluster from terraform destroy. False suits disposable tests; enable for a long-running environment."
  type        = bool
  default     = false
}

variable "gitops_controller" {
  description = "GitOps controller for this disposable environment: argocd or flux. Choose before creation; changing an existing environment is not an ownership migration."
  type        = string
  default     = "argocd"

  validation {
    condition     = contains(["argocd", "flux"], var.gitops_controller)
    error_message = "gitops_controller must be argocd or flux, never both for the same Polyad release."
  }
}

variable "flux_chart_version" {
  description = "Pinned fluxcd-community flux2 chart for the test environment. 2.19.0 includes Flux 2.9.1 and HelmRelease CEL health checks."
  type        = string
  default     = "2.19.0"
}

variable "flux_revision_type" {
  description = "Interpret polyad_revision as a Flux GitRepository branch, tag, or full commit SHA. Ignored by Argo CD."
  type        = string
  default     = "branch"

  validation {
    condition     = contains(["branch", "tag", "commit"], var.flux_revision_type)
    error_message = "flux_revision_type must be branch, tag, or commit."
  }

  validation {
    condition     = var.flux_revision_type != "commit" || can(regex("^[a-fA-F0-9]{40}$", var.polyad_revision))
    error_message = "For a Flux commit reference, polyad_revision must be a full 40-character Git SHA."
  }
}

variable "flux_reconcile_interval" {
  description = "Flux source and Helm reconciliation interval, as a positive duration in seconds, minutes, or hours."
  type        = string
  default     = "1m"

  validation {
    condition     = can(regex("^[1-9][0-9]*(s|m|h)$", var.flux_reconcile_interval))
    error_message = "Use a positive interval such as 30s, 1m, or 1h."
  }
}

variable "argocd_chart_version" {
  description = "Pinned Argo CD Helm chart version. 10.9.2 (Argo CD v3.5.3) was the latest published chart on 2026-09-17."
  type        = string
  default     = "10.9.2"
}

variable "argocd_admin_password_hash" {
  description = "Bcrypt hash of your chosen Argo CD admin password, generated with argocd account bcrypt --password. Supply through TF_VAR_argocd_admin_password_hash."
  type        = string
  sensitive   = true
  default     = null

  validation {
    condition     = var.gitops_controller != "argocd" || can(regex("^\\$2[aby]\\$[0-9]{2}\\$[./A-Za-z0-9]{53}$", var.argocd_admin_password_hash))
    error_message = "Argo CD requires a bcrypt hash, not plaintext. Use argocd account bcrypt --password. Flux does not require this credential."
  }
}

variable "argocd_admin_password_mtime" {
  description = "Stable RFC3339 password modification time. Advance this when changing the password to invalidate existing admin sessions."
  type        = string
  default     = "2026-09-17T00:00:00Z"

  validation {
    condition     = can(formatdate("YYYY", var.argocd_admin_password_mtime))
    error_message = "Use an RFC3339 timestamp, such as 2026-09-17T12:00:00Z."
  }
}

variable "polyad_revision" {
  description = "Public repository branch, tag or commit the selected controller follows. For Flux, also set flux_revision_type when not using a branch."
  type        = string
  default     = "main"
}

variable "polyad_values_files" {
  description = "Ordered values files, relative to charts/polyad in the selected Git revision. The default enables self-managed components, KEDA and internal demo APIs."
  type        = list(string)
  default     = ["../../terraform/polyad-values.yaml"]
}

variable "polyad_values_override" {
  description = "Optional final YAML overrides for experiment parameters or a published operator image. Keep secrets out: stored in the Application or HelmRelease."
  type        = string
  default     = "{}"

  validation {
    condition     = can(keys(yamldecode(var.polyad_values_override)))
    error_message = "polyad_values_override must be a YAML mapping."
  }
}

variable "polyad_automated_sync" {
  description = "Automatically reconcile Polyad from Git. False leaves Argo manual or suspends the Flux HelmRelease, including its initial install. Drain application boundaries before teardown."
  type        = bool
  default     = true
}

variable "fixtures_min_nodes" {
  description = "Minimum and initial nodes for fixture consumers and benchmark support services; independent of the operator pool."
  type        = number
  default     = 1

  validation {
    condition     = var.fixtures_min_nodes >= 1 && floor(var.fixtures_min_nodes) == var.fixtures_min_nodes
    error_message = "fixtures_min_nodes must be a positive integer."
  }
}

variable "fixtures_max_nodes" {
  description = "Autoscaler ceiling for fixture consumers and benchmark support services; must be at least the minimum."
  type        = number
  default     = 10

  validation {
    condition     = var.fixtures_max_nodes >= var.fixtures_min_nodes && floor(var.fixtures_max_nodes) == var.fixtures_max_nodes
    error_message = "fixtures_max_nodes must be an integer at least fixtures_min_nodes."
  }
}

variable "copolyad_min_nodes" {
  description = "Minimum and initial nodes for load generators, isolated from fixture consumers; independent of the operator pool."
  type        = number
  default     = 1

  validation {
    condition     = var.copolyad_min_nodes >= 1 && floor(var.copolyad_min_nodes) == var.copolyad_min_nodes
    error_message = "copolyad_min_nodes must be a positive integer."
  }
}

variable "copolyad_max_nodes" {
  description = "Autoscaler ceiling for load generators, isolated from fixture consumers; must be at least the minimum."
  type        = number
  default     = 10

  validation {
    condition     = var.copolyad_max_nodes >= var.copolyad_min_nodes && floor(var.copolyad_max_nodes) == var.copolyad_max_nodes
    error_message = "copolyad_max_nodes must be an integer at least copolyad_min_nodes."
  }
}

variable "benchmarks_enabled" {
  description = "Register the benchmark Application or HelmRelease. Manual/suspended by default; registration and sync do not start load generation."
  type        = bool
  default     = true
}

variable "benchmarks_values_files" {
  description = "Ordered values paths relative to charts/polyad-benchmarks in polyad_revision; choose a test profile before the GKE placement overlay."
  type        = list(string)
  default     = ["values-smoke.yaml", "../../studies/load/fixtures/gke-values.yaml"]
}

variable "benchmarks_values_override" {
  description = "Final benchmark YAML mapping, for published fixture/runner images or run settings. Stored in the Application or HelmRelease; exclude secrets."
  type        = string
  default     = "{}"

  validation {
    condition     = can(keys(yamldecode(var.benchmarks_values_override)))
    error_message = "benchmarks_values_override must be a YAML mapping."
  }
}

variable "benchmarks_automated_sync" {
  description = "Allow automatic fixture sync and pruning; false lets administrators prepare images and monitoring CRDs before manually syncing. Neither mode activates load tests."
  type        = bool
  default     = false
}
