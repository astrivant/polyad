resource "google_project_service" "required" {
  for_each = toset(["compute.googleapis.com", "container.googleapis.com", "iam.googleapis.com"])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_compute_network" "cluster" {
  project                 = var.project_id
  name                    = var.name
  auto_create_subnetworks = false
  depends_on              = [google_project_service.required]
}

resource "google_compute_subnetwork" "cluster" {
  project                  = var.project_id
  name                     = var.name
  region                   = var.region
  network                  = google_compute_network.cluster.id
  ip_cidr_range            = "10.10.0.0/20"
  private_ip_google_access = true

  secondary_ip_range {
    range_name    = "pods"
    ip_cidr_range = "10.20.0.0/16"
  }
  secondary_ip_range {
    range_name    = "services"
    ip_cidr_range = "10.30.0.0/20"
  }
}

resource "google_service_account" "nodes" {
  project      = var.project_id
  account_id   = "${var.name}-nodes"
  display_name = "Polyad test cluster nodes"
  depends_on   = [google_project_service.required]
}

resource "google_project_iam_member" "nodes" {
  project = var.project_id
  role    = "roles/container.defaultNodeServiceAccount"
  member  = "serviceAccount:${google_service_account.nodes.email}"
}

resource "google_container_cluster" "cluster" {
  project             = var.project_id
  name                = var.name
  location            = var.zone
  node_locations      = [var.zone]
  network             = google_compute_network.cluster.id
  subnetwork          = google_compute_subnetwork.cluster.id
  deletion_protection = var.deletion_protection

  # GKE requires a pool at creation; replace it with the separately managed pool.
  remove_default_node_pool = true
  initial_node_count       = 1
  networking_mode          = "VPC_NATIVE"
  datapath_provider        = "ADVANCED_DATAPATH"

  node_config {
    machine_type    = "e2-highcpu-2"
    image_type      = "UBUNTU_CONTAINERD"
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
  }
  master_auth {
    client_certificate_config {
      issue_client_certificate = false
    }
  }
  release_channel {
    channel = "REGULAR"
  }
  ip_allocation_policy {
    cluster_secondary_range_name  = "pods"
    services_secondary_range_name = "services"
  }
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }
  cluster_autoscaling {
    enabled = false # Keep the two explicitly managed pools; no auto-provisioning.
  }

  depends_on = [google_project_iam_member.nodes]
}

resource "google_container_node_pool" "default" {
  project        = var.project_id
  name           = "default"
  location       = var.zone
  cluster        = google_container_cluster.cluster.name
  node_locations = [var.zone]
  node_count     = var.default_node_count

  # Untainted capacity for GKE-managed services, separate from the experiment.
  node_config {
    machine_type    = "e2-highcpu-2"
    disk_type       = "pd-balanced"
    disk_size_gb    = 50
    image_type      = "UBUNTU_CONTAINERD"
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
    shielded_instance_config {
      enable_secure_boot = true
    }
  }
  management {
    auto_repair  = true
    auto_upgrade = true
  }
}

resource "google_container_node_pool" "polyad" {
  project            = var.project_id
  name               = "polyad"
  location           = var.zone
  cluster            = google_container_cluster.cluster.name
  node_locations     = [var.zone]
  initial_node_count = 2

  autoscaling {
    total_min_node_count = 2
    total_max_node_count = 10
    location_policy      = "BALANCED"
  }
  node_config {
    machine_type    = var.machine_type
    disk_type       = "pd-balanced"
    disk_size_gb    = 50
    image_type      = "UBUNTU_CONTAINERD"
    service_account = google_service_account.nodes.email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
    shielded_instance_config {
      enable_secure_boot = true
    }
    taint {
      key    = "dedicated"
      value  = "polyad"
      effect = "NO_SCHEDULE"
    }
  }
  management {
    auto_repair  = true
    auto_upgrade = true
  }
  upgrade_settings {
    max_surge       = 0
    max_unavailable = 1
  }
  lifecycle {
    # GKE owns capacity once created; later applies must preserve autoscaling.
    ignore_changes = [initial_node_count]
  }
}
