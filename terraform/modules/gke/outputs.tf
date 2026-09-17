output "name" {
  description = "Name of the GKE cluster."
  value       = google_container_cluster.cluster.name
  depends_on  = [google_container_node_pool.default, google_container_node_pool.polyad]
}

output "endpoint" {
  description = "Public control-plane endpoint for authenticated Terraform and kubectl clients."
  value       = google_container_cluster.cluster.endpoint
  depends_on  = [google_container_node_pool.default, google_container_node_pool.polyad]
}

output "ca_certificate" {
  description = "Base64-encoded cluster CA for TLS verification."
  value       = google_container_cluster.cluster.master_auth[0].cluster_ca_certificate
  depends_on  = [google_container_node_pool.default, google_container_node_pool.polyad]
}
