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
  value       = "kubectl -n argocd port-forward service/argocd-server 8080:443"
}

output "polyad_application" {
  description = "Argo CD Application that follows the public Polyad repository."
  value       = "argocd/polyad"
}

output "benchmarks_application" {
  description = "Manual-sync benchmark Application in the Argo CD UI; null when registration is disabled."
  value       = var.benchmarks_enabled ? "argocd/polyad-benchmarks" : null
}
