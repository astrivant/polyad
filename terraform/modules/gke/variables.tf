variable "project_id" {
  description = "Existing, billing-enabled Google Cloud project."
  type        = string
}

variable "region" {
  description = "Region for the dedicated VPC subnet."
  type        = string
}

variable "zone" {
  description = "Zone for the control plane and both node pools."
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
  description = "Machine type for every node in the dedicated polyad pool."
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
