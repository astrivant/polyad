mock_provider "google" {
  mock_resource "google_container_cluster" {
    defaults = {
      endpoint    = "127.0.0.1"
      master_auth = [{ cluster_ca_certificate = "dGVzdC1jYQ==" }]
    }
  }
}

mock_provider "helm" {}

run "isolated_bounded_pool" {
  command = plan

  module {
    source = "./modules/gke"
  }

  variables {
    project_id = "polyad-test-project"
    name       = "polyad-test"
    region     = "us-central1"
    zone       = "us-central1-a"
  }

  assert {
    condition = (
      google_container_cluster.cluster.remove_default_node_pool &&
      !google_container_cluster.cluster.cluster_autoscaling[0].enabled &&
      google_container_node_pool.default.name == "default" &&
      google_container_node_pool.default.node_count == 1 &&
      google_container_node_pool.default.node_config[0].machine_type == "e2-highcpu-2" &&
      length(google_container_node_pool.default.node_config[0].taint) == 0 &&
      google_container_node_pool.polyad.name == "polyad" &&
      google_container_node_pool.polyad.initial_node_count == 2 &&
      google_container_node_pool.polyad.node_config[0].machine_type == "c3-standard-4" &&
      google_container_node_pool.polyad.autoscaling[0].total_min_node_count == 2 &&
      google_container_node_pool.polyad.autoscaling[0].total_max_node_count == 10
    )
    error_message = "Separate an untainted system pool from Polyad's 2–10 total nodes, without node auto-provisioning."
  }

  assert {
    condition = (
      google_container_node_pool.polyad.upgrade_settings[0].max_surge == 0 &&
      google_container_node_pool.polyad.node_config[0].workload_metadata_config[0].mode == "GKE_METADATA" &&
      google_container_node_pool.polyad.node_config[0].image_type == "UBUNTU_CONTAINERD" &&
      google_container_node_pool.default.node_config[0].image_type == "UBUNTU_CONTAINERD" &&
      google_container_node_pool.polyad.node_config[0].taint[0].key == "dedicated" &&
      google_container_node_pool.polyad.node_config[0].taint[0].value == "polyad" &&
      google_container_node_pool.polyad.node_config[0].taint[0].effect == "NO_SCHEDULE" &&
      google_project_iam_member.nodes.role == "roles/container.defaultNodeServiceAccount"
    )
    error_message = "Use Ubuntu nodes, a dedicated NoSchedule taint, no experiment surge nodes, and Workload Identity."
  }

  assert {
    condition = (
      google_container_node_pool.fixtures.name == "fixtures" &&
      google_container_node_pool.fixtures.initial_node_count == 1 &&
      google_container_node_pool.fixtures.autoscaling[0].total_min_node_count == 1 &&
      google_container_node_pool.fixtures.autoscaling[0].total_max_node_count == 10 &&
      google_container_node_pool.fixtures.node_config[0].machine_type == google_container_node_pool.polyad.node_config[0].machine_type &&
      google_container_node_pool.fixtures.node_config[0].image_type == "UBUNTU_CONTAINERD" &&
      google_container_node_pool.fixtures.node_config[0].taint[0].key == "dedicated" &&
      google_container_node_pool.fixtures.node_config[0].taint[0].value == "fixtures" &&
      google_container_node_pool.fixtures.node_config[0].taint[0].effect == "NO_SCHEDULE" &&
      google_container_node_pool.fixtures.upgrade_settings[0].max_surge == 0
    )
    error_message = "fixtures must autoscale independently on identically sized Ubuntu nodes with its own taint."
  }

  assert {
    condition = (
      google_container_node_pool.copolyad.name == "copolyad" &&
      google_container_node_pool.copolyad.initial_node_count == 1 &&
      google_container_node_pool.copolyad.autoscaling[0].total_min_node_count == 1 &&
      google_container_node_pool.copolyad.autoscaling[0].total_max_node_count == 10 &&
      google_container_node_pool.copolyad.node_config[0].machine_type == google_container_node_pool.polyad.node_config[0].machine_type &&
      google_container_node_pool.copolyad.node_config[0].image_type == "UBUNTU_CONTAINERD" &&
      google_container_node_pool.copolyad.node_config[0].taint[0].key == "dedicated" &&
      google_container_node_pool.copolyad.node_config[0].taint[0].value == "copolyad" &&
      google_container_node_pool.copolyad.node_config[0].taint[0].effect == "NO_SCHEDULE" &&
      google_container_node_pool.copolyad.upgrade_settings[0].max_surge == 0
    )
    error_message = "copolyad must autoscale independently on identically sized Ubuntu nodes with its own taint."
  }
}

run "gitops_bootstrap" {
  command = plan

  variables {
    project_id                 = "polyad-test-project"
    argocd_admin_password_hash = "$2a$12$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
  }

  assert {
    condition = (
      helm_release.argocd.version == "10.9.2" &&
      yamldecode(helm_release.argocd.values[0]).server.service.type == "ClusterIP" &&
      yamldecode(helm_release.argocd.values[0]).global.nodeSelector["cloud.google.com/gke-nodepool"] == "fixtures" &&
      yamldecode(helm_release.argocd.values[0]).global.tolerations[0].value == "fixtures" &&
      yamldecode(helm_release.argocd.values[0]).configs.cm["application.resourceTrackingMethod"] == "annotation" &&
      yamldecode(helm_release.bootstrap.values[0]).revision == "main" &&
      yamldecode(helm_release.bootstrap.values[0]).automatedSync
    )
    error_message = "Bootstrap should install the verified Argo release internally and follow Polyad's public main branch."
  }

  assert {
    condition = (
      yamldecode(helm_release.argocd.values[0]).controller.replicas == 1 &&
      yamldecode(helm_release.argocd.values[0]).server.replicas == 1 &&
      yamldecode(helm_release.argocd.values[0]).repoServer.replicas == 1 &&
      yamldecode(helm_release.argocd.values[0]).applicationSet.replicas == 1 &&
      !yamldecode(helm_release.argocd.values[0]).server.autoscaling.enabled &&
      !yamldecode(helm_release.argocd.values[0]).repoServer.autoscaling.enabled &&
      yamldecode(helm_release.argocd.values[0]).redis.enabled &&
      !yamldecode(helm_release.argocd.values[0])["redis-ha"].enabled &&
      yamldecode(helm_release.bootstrap.values[0]).benchmarks.enabled &&
      !yamldecode(helm_release.bootstrap.values[0]).benchmarks.automatedSync
    )
    error_message = "Use standalone Argo components and manually synced benchmark registration by default."
  }

  assert {
    condition = (
      length(local.repositories) == 9 &&
      alltrue([for repo in values(local.repositories) : !startswith(repo.url, "oci://") && !startswith(repo.url, "file://")]) &&
      length([for repo in values(local.repositories) : repo if try(repo.enableOCI, "false") == "true"]) == 1
    )
    error_message = "Register Polyad Git and remote charts with Argo's OCI URL format; local chart dependencies stay in the Git checkout."
  }

  assert {
    condition = (
      contains(keys(local.polyad_health), "resource.customizations.health.polyad.astrivant.com_Graph") &&
      contains(keys(local.polyad_health), "resource.customizations.health.polyad.astrivant.com_ReplicaGroup") &&
      strcontains(local.health_lua, "local definitions = {Daemon = true") &&
      !strcontains(local.health_lua, "-- REGISTRY_DEFINITIONS")
    )
    error_message = "Install the shared graph health Lua with reusable definitions resolved."
  }
}

run "frozen_experiment" {
  command = plan

  variables {
    project_id                 = "polyad-test-project"
    argocd_admin_password_hash = "$2a$12$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    polyad_revision            = "0123456789012345678901234567890123456789"
    polyad_automated_sync      = false
    polyad_values_override     = "operator:\n  image:\n    tag: experiment-42\n"
  }

  assert {
    condition = (
      yamldecode(helm_release.bootstrap.values[0]).revision == var.polyad_revision &&
      !yamldecode(helm_release.bootstrap.values[0]).automatedSync &&
      yamldecode(yamldecode(helm_release.bootstrap.values[0]).values).operator.image.tag == "experiment-42"
    )
    error_message = "Support immutable revisions, manual sync and explicit experiment image overrides."
  }
}

run "independent_capacity" {
  command = plan
  module { source = "./modules/gke" }
  variables {
    project_id         = "polyad-test-project"
    name               = "polyad-test"
    region             = "us-central1"
    zone               = "us-central1-a"
    machine_type       = "c3-standard-8"
    fixtures_min_nodes = 2
    fixtures_max_nodes = 4
    copolyad_min_nodes = 3
    copolyad_max_nodes = 6
  }
  assert {
    condition = (
      google_container_node_pool.polyad.node_config[0].machine_type == "c3-standard-8" &&
      google_container_node_pool.fixtures.node_config[0].machine_type == "c3-standard-8" &&
      google_container_node_pool.copolyad.node_config[0].machine_type == "c3-standard-8" &&
      google_container_node_pool.fixtures.initial_node_count == 2 &&
      google_container_node_pool.fixtures.autoscaling[0].total_max_node_count == 4 &&
      google_container_node_pool.copolyad.initial_node_count == 3 &&
      google_container_node_pool.copolyad.autoscaling[0].total_max_node_count == 6 &&
      google_container_node_pool.polyad.autoscaling[0].total_max_node_count == 10
    )
    error_message = "One machine type must reach all experiment pools while their capacity limits remain independent."
  }
}

run "invalid_capacity" {
  command = plan
  module { source = "./modules/gke" }
  variables {
    project_id         = "polyad-test-project"
    name               = "polyad-test"
    region             = "us-central1"
    zone               = "us-central1-a"
    fixtures_min_nodes = 3
    fixtures_max_nodes = 2
    copolyad_min_nodes = 2
    copolyad_max_nodes = 1
  }
  expect_failures = [var.fixtures_max_nodes, var.copolyad_max_nodes]
}
