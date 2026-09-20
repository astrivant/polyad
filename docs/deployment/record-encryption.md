# Encrypt PostgreSQL records in the operator

<!-- toc:start -->
**Table of contents**

- [Configure key Secrets](#configure-key-secrets)
- [What is encrypted](#what-is-encrypted)
- [Read encrypted records](#read-encrypted-records)
- [Rotate keys and migrate existing data](#rotate-keys-and-migrate-existing-data)
- [Deployment and failure behavior](#deployment-and-failure-behavior)
<!-- toc:end -->

Set `postgresql.recordEncryption.enabled: true` to encrypt JSON payloads **before
the operator sends them to PostgreSQL**. This is optional, disabled by default,
and works with managed or external state and authentication databases. It can be
combined with [volume encryption](postgresql.md#encryption-at-rest).

## Configure key Secrets

Use [the typed reference values](../../charts/polyad/references/values-postgresql-record-encryption.reference.yaml)
with an existing Secret in the Helm release namespace:

```yaml
postgresql:
  enabled: true
  recordEncryption:
    enabled: true
    existingSecret: polyad-record-keys
    publicKeyKey: public.pem
    privateKeyKey: ''
    privateKeyPasswordKey: ''
```

The chart references keys; it does not generate them or accept PEM material in
values. The public key must be PEM-encoded RSA, at least 2048 bits; use 3072 bits
for a new pair. For example, run locally to create a pair, retain the private key
securely, and provision **only the public key** to writers:

```sh
umask 077
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:3072 -out private.pem
openssl pkey -in private.pem -pubout -out public.pem
kubectl -n polyad create secret generic polyad-record-keys --from-file=public.pem
```

To provide both halves, create the Secret with both `--from-file=public.pem` and
`--from-file=private.pem`, then set `privateKeyKey: private.pem`. Writers need only
the public key; the optional private key enables local decryption through the
Python helper below. If the private PEM is password-protected, also supply a
password file as a Secret entry and name it in `privateKeyPasswordKey`. That file
must contain the exact UTF-8 password, without an unintended trailing newline.

| Field under `postgresql.recordEncryption` | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Encrypt new database JSON payloads; requires state or authentication storage |
| `existingSecret` | Empty | Existing key Secret in this release's namespace |
| `publicKeyKey` | `public.pem` | Required RSA public PEM entry |
| `privateKeyKey` | Empty | Optional matching private PEM entry; empty keeps it off the writer |
| `privateKeyPasswordKey` | Empty | Optional encrypted-PEM password entry; requires a private key entry |

## What is encrypted

| Table / JSON column | Encrypted contents | Authenticated row identity, in order |
| --- | --- | --- |
| `polyad_graph_state.document` | Graph definitions and observed status | `scope`, `cluster`, `namespace`, `kind`, `name`, `uid`, `resource_version` |
| `polyad_namespace_state.snapshot` | Namespace inventory and tracked measurements | `scope`, `cluster`, `namespace` |
| `polyad_event_history.payload` | Archived application events | `scope`, `identity` |
| `polyad_auth_keys.policy` | API-key policy revisions | `scope`, `key_group`, `key_name`, `verifier`, `policy_digest` |

SQL identities, timestamps, ordering fields, revocation flags and one-way token
and policy fingerprints remain readable. They support deduplication, retention,
revocation and connection metrics. Raw bearer tokens are never stored in these
tables. This setting covers PostgreSQL payloads; authorized live event delivery
and Dragonfly's bounded replay remain governed by their existing access controls.

Each write generates a fresh AES-256 key and 12-byte nonce. AES-GCM encrypts and
authenticates the JSON; RSA-OAEP with SHA-256 wraps the AES key. The operator uses
the maintained Python `cryptography` implementations of
[AES-GCM](https://cryptography.io/en/stable/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM)
and [RSA-OAEP](https://cryptography.io/en/stable/hazmat/primitives/asymmetric/rsa/#encryption).

The JSON column holds a versioned envelope with `format`, `version`, `algorithm`,
`keyId`, `wrappedKey`, `nonce` and `ciphertext`. `keyId` is the SHA-256 fingerprint
of the public key's DER SubjectPublicKeyInfo. Format, version, algorithm, key
fingerprint, table and row identity form the authenticated context. Moving a
payload to another row fails decryption. This detects changes to an envelope;
it does not replace database authorization or prevent replay of an older valid
payload at the same identity. Anyone possessing the public key can encrypt new
data, so encryption alone does not authenticate the writer.

## Read encrypted records

The operator currently rebuilds observations from Kubernetes and does not read
these JSON columns to authorize requests or recover live controller state.
Encrypting them therefore does not require private keys on writers. Database
inspection and recovery tooling can import `RecordCipher` with the matching pair:

```python
from pathlib import Path
from polyad.sql.encryption import RecordCipher

reader = RecordCipher(
    Path("public.pem").read_bytes(),
    Path("private.pem").read_bytes(),
)
# row is a mapping returned by SELECT * FROM polyad_graph_state ...
identity = tuple(row[field] for field in (
    "scope", "cluster", "namespace", "kind", "name", "uid", "resource_version",
))
document = reader.decrypt(row["document"], "polyad_graph_state", identity)
```

Pass `password=Path("password").read_bytes()` for an encrypted private PEM.
Use the identity ordering in the table for other record types. The helper rejects
wrong keys, tampered envelopes and incorrect row context; it never treats a failed
decryption as plaintext. There is no endpoint exposing private keys or decrypting
records for downstream clients.

## Rotate keys and migrate existing data

Keep every private key needed to read retained records and backups. A new public
key changes `keyId` for future writes; older ciphertext still needs its original
private key. Restart all writers after changing their Secret, and allow overlapping
old/new key IDs during a rolling replacement. The existing operator credential
health checks also detect changed loaded key files. Observers should be rolled
explicitly, or through the configured Secret-change reloader.

**Enablement does not rewrite existing rows.** Successful new scans replace current
graph and namespace snapshots; existing event and authentication-policy revisions
can persist unchanged because their insertions are deduplicated. For complete
conversion, pause writers and use controlled migration tooling to encrypt each
JSON column with its table and original row identity. Retain old keys until old
rows and backups have been migrated or retired. Do not double-encrypt existing
envelopes during migration. The chart does not run a bulk data migration.

Disabling the option affects future writes only; it does not decrypt existing
records. PostgreSQL dumps preserve encrypted payloads, but readable metadata,
older plaintext rows and other backup contents still require an appropriate
backup encryption policy.

## Deployment and failure behavior

Configure every writer consistently: dense HA replicas, distributed components,
database-backed observers and independently installed workers. Root-provisioned
workers inherit state-write encryption when state storage is enabled; workers
without database writes receive no record keys. Only selected Secret entries are
copied downstream. Mounting a private key deliberately makes it available to those
operator processes as well; public-only deployment is sufficient for persistence.

Key files are projected read-only, with no PEM content in environment variables,
Helm values, PostgreSQL, Dragonfly or application logs. Missing, invalid, weak or
mismatched configured keys stop the writer from opening its database pool. An
encryption failure aborts the database write; it never retries in plaintext.

`cryptography` is included in the operator's Python dependencies and production
image. Its record-encryption module is imported only when this feature is enabled
or an administrator explicitly imports the decryption helper. Connection metrics,
KEDA replica scaling and PostgreSQL volume encryption keep their existing behavior.
