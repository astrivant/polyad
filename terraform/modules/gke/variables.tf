variable "project_id" {
  description = "Billing-enabled project created by the bootstrap module, with IAM enabled."
  type        = string
}

variable "region" {
  description = "Region for the dedicated VPC subnet."
  type        = string
}

variable "zone" {
  description = "Zone for the control plane and all four node pools."
  type        = string
}

variable "name" {
  description = "Cluster name and network prefix, at most 24 characters."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{0,22}[a-z0-9]$", var.name))
    error_message = "Use a 2–24 character lowercase cluster name so the node service-account ID also fits."
  }
}

variable "machine_type" {
  description = "Shared machine type for the polyad operator, fixtures consumers/support and copolyad load-generator pools."
  type        = string
  default     = "c3-standard-4"
}

variable "default_node_count" {
  description = "Fixed untainted e2-highcpu-2 nodes for GKE services, separate from the 2–10-node Polyad pool."
  type        = number
  default     = 1

  validation {
    condition     = var.default_node_count >= 1 && var.default_node_count <= 10 && floor(var.default_node_count) == var.default_node_count
    error_message = "default_node_count must be an integer from 1 to 10."
  }
}

variable "deletion_protection" {
  description = "Prevent accidental cluster deletion when true."
  type        = bool
  default     = false
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
