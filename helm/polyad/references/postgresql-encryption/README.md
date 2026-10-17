# postgresql-encryption configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Guide: ../../../../docs/deployment/postgresql.md#encryption-at-rest

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/postgresql-encryption/values.yaml
```

## Parameters

### Configuration

| Name                                       | Description                                                                                                                                             | Value                                                                                      |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `postgresql.enabled`                       | **Type: boolean.** Enable the state database; encryption also covers event history stored there.                                                        | `true` |
| `postgresql.managed`                       | **Type: boolean.** Create a CloudNativePG Cluster; external databases configure encryption with their provider.                                         | `true` |
| `postgresql.encryptionAtRest.enabled`      | **Type: boolean.** Apply this selection to state and separately enabled managed authentication databases; does not rewrite existing disks.              | `true` |
| `postgresql.encryptionAtRest.provider`     | **Type: string.** GKE creates the class below; choose ExistingStorageClass and clear kmsKeyName to use an administrator-verified class on any provider. | `GKE` |
| `postgresql.encryptionAtRest.storageClass` | **Type: string.** Empty generates a release-specific GKE class name; ExistingStorageClass requires a nonempty existing class name.                      | `""` |
| `postgresql.encryptionAtRest.kmsKeyName`   | **Type: string.** Replace with a symmetric CryptoKey in the cluster's region; use the key resource without a cryptoKeyVersions suffix.                  | `projects/replace-key-project/locations/us-central1/keyRings/polyad/cryptoKeys/postgresql` |
| `postgresql.encryptionAtRest.diskType`     | **Type: string.** Choose a disk type supported by your machine family and CSI driver; this only affects newly provisioned volumes.                      | `pd-balanced` |
