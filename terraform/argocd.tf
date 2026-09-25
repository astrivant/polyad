locals {
  charts = [for name in ["polyad", "polyad-benchmarks"] :
    yamldecode(file("${path.module}/../charts/${name}/Chart.yaml"))
  ]

  # Register remote dependencies, including currently disabled optional charts.
  # Local file:// dependencies are supplied by the Git checkout.
  # Argo's OCI repository URL omits oci:// and uses enableOCI instead.
  repositories = merge({
    polyad = { type = "git", url = "https://github.com/astrivant/polyad.git" }
    argo   = { type = "helm", name = "argo", url = "https://argoproj.github.io/argo-helm" }
    }, {
    for url in toset(flatten([for chart in local.charts : [for dependency in chart.dependencies : dependency.repository]])) :
    "polyad-${substr(sha256(url), 0, 12)}" => {
      type      = "helm"
      name      = "polyad-${substr(sha256(url), 0, 12)}"
      url       = trimprefix(url, "oci://")
      enableOCI = tostring(startswith(url, "oci://"))
    }
    if !startswith(url, "file://")
  })

  # Kept in step with the Python resource registry by test_terraform.py.
  definition_kinds = ["Daemon", "Gate", "GraphPolicy", "Resource", "ShutdownPolicy", "Workload"]
  health_lua = replace(
    file("${path.module}/../integrations/argocd/health.lua"),
    "-- REGISTRY_DEFINITIONS",
    "local definitions = {${join(", ", [for kind in local.definition_kinds : "${kind} = true"])}}"
  )
  crds = [for filename in fileset("${path.module}/../charts/polyad-crds/crds", "*.yaml") :
    yamldecode(file("${path.module}/../charts/polyad-crds/crds/${filename}"))
  ]
  polyad_health = {
    for crd in local.crds :
    "resource.customizations.health.${crd.spec.group}_${crd.spec.names.kind}" => local.health_lua
    if crd.spec.group == "polyad.astrivant.com"
  }
}
