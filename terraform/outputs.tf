output "cluster_name" {
  description = "GKE cluster created for experiments."
  value       = module.gke.name
}

output "get_credentials_command" {
  description = "Configure kubectl for the new test cluster."
  value       = "gcloud container clusters get-credentials ${module.gke.name} --zone ${var.zone} --project ${var.project_id}"
}

output "argocd_port_forward_command" {
  description = "Access Argo CD at https://localhost:8080 with username admin and your chosen password."
  value       = var.gitops_controller == "argocd" ? "kubectl -n argocd port-forward service/argocd-server 8080:443" : null
}

output "polyad_application" {
  description = "Argo CD Application that follows the public Polyad repository."
  value       = var.gitops_controller == "argocd" ? "argocd/polyad" : null
}

output "benchmarks_application" {
  description = "Manual-sync benchmark Application in the Argo CD UI; null when registration is disabled."
  value       = var.gitops_controller == "argocd" && var.benchmarks_enabled ? "argocd/polyad-benchmarks" : null
}

output "gitops_controller" {
  description = "Controller owning Polyad in this environment."
  value       = var.gitops_controller
}

output "flux_status_command" {
  description = "Inspect Flux source and release readiness; null for Argo CD."
  value       = var.gitops_controller == "flux" ? "kubectl -n flux-system get gitrepositories,helmreleases" : null
}

output "polyad_helmrelease" {
  description = "Flux HelmRelease following the public Polyad repository; null for Argo CD."
  value       = var.gitops_controller == "flux" ? "flux-system/polyad" : null
}

output "benchmarks_helmrelease" {
  description = "Initially suspended benchmark HelmRelease; null for Argo CD or disabled benchmarks."
  value       = var.gitops_controller == "flux" && var.benchmarks_enabled ? "flux-system/polyad-benchmarks" : null
}
