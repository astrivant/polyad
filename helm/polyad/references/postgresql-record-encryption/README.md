# postgresql-record-encryption configuration

<!-- toc:start -->
**Table of contents**

- [Parameters](#parameters)
  - [Configuration](#configuration)
<!-- toc:end -->

Guide: ../../../../docs/deployment/postgresql.md#encrypt-records-before-writing

Apply this overlay with the chart defaults; review its settings before deployment.

```sh
helm template polyad helm/polyad --values helm/polyad/references/postgresql-record-encryption/values.yaml
```

## Parameters

### Configuration

| Name                                                | Description                                                                                                                                     | Value                |
| --------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- | -------------------- |
| `postgresql.enabled`                                | **Type: boolean.** Enable durable state and event history; authentication-only deployments can instead enable authentication.storage.           | `true` |
| `postgresql.recordEncryption.enabled`               | **Type: boolean.** Encrypt new records in every writer; existing rows, replicas running old settings and backups are not retroactively changed. | `true` |
| `postgresql.recordEncryption.existingSecret`        | **Type: string.** Replace with your existing key Secret; the chart never creates or embeds key material in Helm values.                         | `polyad-record-keys` |
| `postgresql.recordEncryption.publicKeyKey`          | **Type: string.** Secret entry containing a PEM RSA public key; 3072 bits recommended, 2048 minimum.                                            | `public.pem` |
| `postgresql.recordEncryption.privateKeyKey`         | **Type: string.** Empty keeps the private key off writers; set private.pem to mount a matching key pair for local decryption.                   | `""` |
| `postgresql.recordEncryption.privateKeyPasswordKey` | **Type: string.** Optional Secret entry containing the exact password for an encrypted private PEM; requires privateKeyKey.                     | `""` |
