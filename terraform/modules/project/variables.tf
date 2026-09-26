variable "project_id" {
  description = "Globally unique project ID; its display name is always polyad."
  type        = string
}

variable "organization_id" {
  description = "Parent organization ID."
  type        = string
}

variable "billing_account_id" {
  description = "Billing account authorized for the bootstrap identity."
  type        = string
}

variable "bootstrap_service_account" {
  description = "Organization-authorized service account allowed to impersonate the new project deployer."
  type        = string
}

variable "deletion_policy" {
  description = "PREVENT guards project destruction; DELETE explicitly permits disposable-project cleanup."
  type        = string
  default     = "PREVENT"
}
