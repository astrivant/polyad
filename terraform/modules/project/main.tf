resource "google_project" "polyad" {
  name                = "polyad"
  project_id          = var.project_id
  org_id              = var.organization_id
  billing_account     = var.billing_account_id
  auto_create_network = false
  deletion_policy     = var.deletion_policy
}

resource "google_project_service" "bootstrap" {
  # Compute and Container APIs remain owned by the GKE module, avoiding duplicate
  # Terraform ownership of the same enabled service across the two phases.
  for_each = toset(["iam.googleapis.com", "iamcredentials.googleapis.com", "serviceusage.googleapis.com", "cloudresourcemanager.googleapis.com"])

  project            = google_project.polyad.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_service_account" "deployer" {
  project      = google_project.polyad.project_id
  account_id   = "polyad-terraform"
  display_name = "Polyad project infrastructure deployer"
  depends_on   = [google_project_service.bootstrap]
}

resource "google_project_iam_member" "deployer" {
  # No organization or billing grants, and no blanket Owner/Editor role.
  # Project IAM administration is needed to authorize the separate GKE node SA.
  for_each = toset([
    "roles/compute.networkAdmin",
    "roles/container.admin",
    "roles/iam.serviceAccountAdmin",
    "roles/iam.serviceAccountUser",
    "roles/resourcemanager.projectIamAdmin",
    "roles/serviceusage.serviceUsageAdmin",
  ])

  project = google_project.polyad.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.deployer.email}"
}

resource "google_service_account_iam_member" "bootstrap_impersonation" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${var.bootstrap_service_account}"
}
