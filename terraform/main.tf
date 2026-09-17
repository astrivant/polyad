provider "google" {
  project = var.project_id
  region  = var.region
  zone    = var.zone
}

module "gke" {
  source = "./modules/gke"

  project_id          = var.project_id
  region              = var.region
  zone                = var.zone
  name                = var.cluster_name
  machine_type        = var.machine_type
  default_node_count  = var.default_node_count
  deletion_protection = var.deletion_protection
}

# Refresh credentials on demand, including long cluster creates and later destroys.
# gke-gcloud-auth-plugin uses the same Application Default Credentials as Google.
provider "helm" {
  kubernetes = {
    host                   = "https://${module.gke.endpoint}"
    cluster_ca_certificate = base64decode(module.gke.ca_certificate)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "gke-gcloud-auth-plugin"
      args        = ["--use_application_default_credentials"]
    }
  }
}

resource "helm_release" "argocd" {
  name             = "argocd"
  namespace        = "argocd"
  create_namespace = true
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = var.argocd_chart_version
  atomic           = true
  timeout          = 900

  values = [yamlencode({
    global = {
      nodeSelector = { "cloud.google.com/gke-nodepool" = "polyad" }
      tolerations  = [{ key = "dedicated", operator = "Equal", value = "polyad", effect = "NoSchedule" }]
    }
    dex           = { enabled = false }
    notifications = { enabled = false }
    server        = { service = { type = "ClusterIP" } }
    configs = {
      cm = merge(local.polyad_health, {
        "admin.enabled"                      = true
        "application.resourceTrackingMethod" = "annotation"
      })
      repositories = local.repositories
      secret       = { argocdServerAdminPasswordMtime = var.argocd_admin_password_mtime }
    }
  })]

  set_sensitive = [{
    name  = "configs.secret.argocdServerAdminPassword"
    value = var.argocd_admin_password_hash
  }]

  depends_on = [module.gke]
}

# A small Helm release defers Application creation until Argo CRDs exist.
# kubernetes_manifest would require those CRDs during the initial plan.
resource "helm_release" "bootstrap" {
  name      = "polyad-bootstrap"
  namespace = helm_release.argocd.namespace
  chart     = "${path.module}/bootstrap"
  atomic    = true
  timeout   = 300

  values = [yamlencode({
    revision      = var.polyad_revision
    valueFiles    = var.polyad_values_files
    values        = var.polyad_values_override
    automatedSync = var.polyad_automated_sync
  })]

  depends_on = [helm_release.argocd]
}
