output "project_id" {
  description = "Created project ID after API and deployer authorization setup."
  value       = google_project.polyad.project_id
  depends_on  = [google_project_iam_member.deployer, google_service_account_iam_member.bootstrap_impersonation]
}

output "deployer_email" {
  description = "Impersonatable deployer identity, not a private key or access token."
  value       = google_service_account.deployer.email
  depends_on  = [google_project_iam_member.deployer, google_service_account_iam_member.bootstrap_impersonation]
}
