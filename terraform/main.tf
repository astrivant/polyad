provider "google" {
  credentials                 = local.bootstrap_credentials_path
  impersonate_service_account = module.project.deployer_email
  project                     = module.project.project_id
  region                      = var.region
  zone                        = var.zone
}

module "gke" {
  source = "./modules/gke"

  project_id          = module.project.project_id
  region              = var.region
  zone                = var.zone
  name                = var.cluster_name
  machine_type        = var.machine_type
  default_node_count  = var.default_node_count
  fixtures_min_nodes  = var.fixtures_min_nodes
  fixtures_max_nodes  = var.fixtures_max_nodes
  copolyad_min_nodes  = var.copolyad_min_nodes
  copolyad_max_nodes  = var.copolyad_max_nodes
  deletion_protection = var.deletion_protection

  # Credentials and IAM grants must survive until every project resource is gone.
  depends_on = [module.project]
}

# Refresh credentials on demand, including long cluster creates and later destroys.
# ADC mode ignores the plugin's impersonation flag. Use gcloud's credential-file
# override and explicit impersonation so Helm never falls back to the root identity.
provider "helm" {
  kubernetes = {
    host                   = "https://${module.gke.endpoint}"
    cluster_ca_certificate = base64decode(module.gke.ca_certificate)
    exec = {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "gke-gcloud-auth-plugin"
      args = [
        "--account=${local.bootstrap_service_account}",
        "--impersonate_service_account=${module.project.deployer_email}",
      ]
      env = {
        CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE = local.bootstrap_credentials_path
        # An inherited pre-minted token would bypass impersonation in the plugin.
        CLOUDSDK_AUTH_ACCESS_TOKEN = ""
      }
    }
  }
}

# Preserve existing Argo installations when making the resource optional.
moved {
  from = helm_release.argocd
  to   = helm_release.argocd[0]
}

resource "helm_release" "argocd" {
  count = var.gitops_controller == "argocd" ? 1 : 0

  name             = "argocd"
  namespace        = "argocd"
  create_namespace = true
  repository       = "https://argoproj.github.io/argo-helm"
  chart            = "argo-cd"
  version          = var.argocd_chart_version
  atomic           = true
  timeout          = 900

  values = [yamlencode(merge(yamldecode(file("${path.module}/argocd-values.yaml")), {
    configs = {
      cm = merge(local.polyad_health, {
        "admin.enabled"                      = true
        "application.resourceTrackingMethod" = "annotation"
      })
      repositories = local.repositories
      secret       = { argocdServerAdminPasswordMtime = var.argocd_admin_password_mtime }
    }
  }))]

  set_sensitive = [{
    name  = "configs.secret.argocdServerAdminPassword"
    value = var.argocd_admin_password_hash
  }]

  depends_on = [module.gke]
}

# A small Helm release defers GitOps resources until their controller CRDs exist.
# kubernetes_manifest would require those CRDs during the initial plan.
resource "helm_release" "bootstrap" {
  name      = "polyad-bootstrap"
  namespace = var.gitops_controller == "argocd" ? "argocd" : "flux-system"
  chart     = "${path.module}/bootstrap"
  atomic    = true
  timeout   = 300

  values = [yamlencode({
    controller    = var.gitops_controller
    revision      = var.polyad_revision
    valueFiles    = var.polyad_values_files
    values        = var.polyad_values_override
    automatedSync = var.polyad_automated_sync
    flux = {
      revisionType = var.flux_revision_type
      interval     = var.flux_reconcile_interval
    }
    benchmarks = {
      enabled       = var.benchmarks_enabled
      valueFiles    = var.benchmarks_values_files
      values        = var.benchmarks_values_override
      automatedSync = var.benchmarks_automated_sync
    }
  })]

  depends_on = [helm_release.argocd, helm_release.flux]
}
