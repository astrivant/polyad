# Flux documents this community-maintained chart as a development install option:
# https://fluxcd.io/flux/installation/#dev-install
# Reuse the locked Helm provider; no Git credentials or repository writes are needed.
resource "helm_release" "flux" {
  count = var.gitops_controller == "flux" ? 1 : 0

  name             = "flux"
  namespace        = "flux-system"
  create_namespace = true
  repository       = "oci://ghcr.io/fluxcd-community/charts"
  chart            = "flux2"
  version          = var.flux_chart_version
  atomic           = true
  wait             = true
  timeout          = 900

  # Keep GitOps support off the measured operator and load-generator pools.
  # Image automation is intentionally absent: tests must not write back to Git.
  values = [yamlencode(merge({
    installCRDs               = true
    imageAutomationController = { create = false }
    imageReflectionController = { create = false }
    sourceWatcher             = { create = false }
    }, {
    for controller in ["sourceController", "helmController", "kustomizeController", "notificationController"] :
    controller => {
      create       = true
      nodeSelector = { "cloud.google.com/gke-nodepool" = "fixtures" }
      tolerations  = [{ key = "dedicated", operator = "Equal", value = "fixtures", effect = "NoSchedule" }]
    }
  }))]

  depends_on = [module.gke]
}
