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

variable "argocd_chart_version" {
  description = "Pinned Argo CD Helm chart version. 10.9.2 (Argo CD v3.5.3) was the latest published chart on 2026-09-17."
  type        = string
  default     = "10.9.2"
}

variable "argocd_admin_password_hash" {
  description = "Bcrypt hash of your chosen Argo CD admin password, generated with argocd account bcrypt --password. Supply through TF_VAR_argocd_admin_password_hash."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^\\$2[aby]\\$[0-9]{2}\\$[./A-Za-z0-9]{53}$", var.argocd_admin_password_hash))
    error_message = "Provide a bcrypt hash, not a plaintext password. Use argocd account bcrypt --password with your chosen password."
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
  description = "Public repository branch, tag or commit Argo CD follows. Use main for continuous updates or a commit SHA for a repeatable experiment."
  type        = string
  default     = "main"
}

variable "polyad_values_files" {
  description = "Ordered values files, relative to charts/polyad in the selected Git revision. The default enables self-managed components, KEDA and internal demo APIs."
  type        = list(string)
  default     = ["../../terraform/polyad-values.yaml"]
}

variable "polyad_values_override" {
  description = "Optional final YAML overrides for experiment parameters or a published operator image. Keep secrets out: this text is stored in the Argo Application."
  type        = string
  default     = "{}"

  validation {
    condition     = can(keys(yamldecode(var.polyad_values_override)))
    error_message = "polyad_values_override must be a YAML mapping."
  }
}

variable "polyad_automated_sync" {
  description = "Automatically sync, prune and self-heal Polyad from Git. Disable while freezing an experiment or performing ordered teardown."
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
  description = "Register the benchmark Application in Argo CD. Manual sync by default; registration and sync do not start load generation."
  type        = bool
  default     = true
}

variable "benchmarks_values_files" {
  description = "Ordered values paths relative to charts/polyad-benchmarks in polyad_revision; choose a test profile before the GKE placement overlay."
  type        = list(string)
  default     = ["values-smoke.yaml", "../../studies/load/gke-values.yaml"]
}

variable "benchmarks_values_override" {
  description = "Final benchmark YAML mapping, for published fixture/runner images or run settings. Stored in the Application; exclude secrets."
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
