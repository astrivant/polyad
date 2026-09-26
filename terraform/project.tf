locals {
  bootstrap_credentials_path = abspath(pathexpand(var.bootstrap_credentials_file))
  # Only non-secret identity metadata leaves the credential file. Never create
  # a service_account_key resource or persist project access tokens in state.
  bootstrap_service_account = jsondecode(file(local.bootstrap_credentials_path)).client_email
  bootstrap_project_id      = jsondecode(file(local.bootstrap_credentials_path)).project_id
}

# Bootstrap uses the supplied organization-authorized identity, not the deployer
# which does not exist yet. Enable Resource Manager, Billing, Service Usage, IAM,
# and IAM Credentials APIs in this identity's own project before running Terraform.
provider "google" {
  alias       = "bootstrap"
  credentials = local.bootstrap_credentials_path
  project     = local.bootstrap_project_id
}

module "project" {
  source    = "./modules/project"
  providers = { google = google.bootstrap }

  project_id                = var.project_id
  organization_id           = var.organization_id
  billing_account_id        = var.billing_account_id
  bootstrap_service_account = local.bootstrap_service_account
  deletion_policy           = var.project_deletion_policy
}
