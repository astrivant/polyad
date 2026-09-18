"""
Verify encrypted volume selection for managed PostgreSQL state and authentication storage.
"""

from __future__ import annotations

import json
import subprocess

import jsonschema
import pytest
import yaml

from tests.test_chart import CHART, render
from tests.test_chart import pytestmark as pytestmark


def test_existing_encrypted_class_covers_both_databases_and_shared_authentication():
    """
    Every primary and standby uses the selected class without creating an unverified new class.
    """
    objects = render(
        "postgresql.enabled=true",
        "postgresql.ha.enabled=true",
        "postgresql.encryptionAtRest.enabled=true",
        "postgresql.encryptionAtRest.storageClass=encrypted-database",
        "authentication.storage.enabled=true",
        values_files=(CHART / "values-authentication.reference.yaml",),
    )
    clusters = [obj for obj in objects if obj["kind"] == "Cluster"]
    assert {obj["metadata"]["name"] for obj in clusters} == {"test-state", "test-authentication"}
    assert all(obj["spec"]["storage"]["storageClass"] == "encrypted-database" for obj in clusters)
    assert all(obj["spec"]["instances"] == 3 for obj in clusters)
    assert not any(obj["kind"] == "StorageClass" for obj in objects)
    shared = render(
        "postgresql.enabled=true",
        "postgresql.encryptionAtRest.enabled=true",
        "postgresql.encryptionAtRest.storageClass=encrypted-database",
        "authentication.storage.enabled=true",
        "authentication.storage.separateDatabase=false",
        values_files=(CHART / "values-authentication.reference.yaml",),
    )
    assert [obj["metadata"]["name"] for obj in shared if obj["kind"] == "Cluster"] == ["test-state"]


@pytest.mark.parametrize("state_enabled", [True, False])
def test_gke_cmek_class_and_keda_replicas_preserve_encrypted_storage(state_enabled):
    """
    The reference renders real CSI encryption parameters without handing KMS material to the operator.
    """
    objects = render(
        f"postgresql.enabled={str(state_enabled).lower()}",
        "postgresql.ha.enabled=true",
        f"postgresql.autoscaling.enabled={str(state_enabled).lower()}",
        "authentication.storage.enabled=true",
        values_files=(CHART / "values-authentication.reference.yaml", CHART / "values-postgresql-encryption.reference.yaml"),
    )
    storage = next(obj for obj in objects if obj["kind"] == "StorageClass")
    assert storage["metadata"]["name"] == "test-test-pg-encrypted"
    assert storage["provisioner"] == "pd.csi.storage.gke.io"
    assert storage["parameters"] == {
        "type": "pd-balanced",
        "disk-encryption-kms-key": "projects/replace-key-project/locations/us-central1/keyRings/polyad/cryptoKeys/postgresql",
    }
    assert storage["reclaimPolicy"] == "Retain"
    assert storage["volumeBindingMode"] == "WaitForFirstConsumer"
    assert storage["allowVolumeExpansion"] is True
    assert storage["metadata"]["annotations"]["helm.sh/resource-policy"] == "keep"
    clusters = [obj for obj in objects if obj["kind"] == "Cluster"]
    assert len(clusters) == (2 if state_enabled else 1)
    assert all(obj["spec"]["storage"]["storageClass"] == storage["metadata"]["name"] for obj in clusters)
    assert all(obj["spec"]["instances"] == 3 for obj in clusters)
    assert all(storage["parameters"]["disk-encryption-kms-key"] not in json.dumps(obj) for obj in objects if obj != storage)
    if state_enabled:
        scale = next(obj for obj in objects if obj["kind"] == "ScaledObject" and obj["metadata"]["name"] == "test-state")
        assert scale["spec"]["scaleTargetRef"]["name"] == "test-state"


def test_explicit_matching_classes_and_gke_name_are_accepted():
    """
    Administrators may use a deliberate class name with consistent database overrides.
    """
    objects = render(
        "postgresql.encryptionAtRest.storageClass=managed-encrypted",
        "postgresql.storage.storageClass=managed-encrypted",
        "authentication.storage.enabled=true",
        "authentication.storage.storageClass=managed-encrypted",
        values_files=(CHART / "values-authentication.reference.yaml", CHART / "values-postgresql-encryption.reference.yaml"),
    )
    assert next(obj for obj in objects if obj["kind"] == "StorageClass")["metadata"]["name"] == "managed-encrypted"
    assert all(obj["spec"]["storage"]["storageClass"] == "managed-encrypted" for obj in objects if obj["kind"] == "Cluster")


@pytest.mark.parametrize("path", ["postgresql.storage.storageClass", "authentication.storage.storageClass"])
def test_conflicting_database_class_cannot_bypass_encrypted_selection(path):
    """
    A leftover database-specific override cannot silently select another storage backend.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(
            f"{path}=different-storage",
            "authentication.storage.enabled=true",
            values_files=(CHART / "values-authentication.reference.yaml", CHART / "values-postgresql-encryption.reference.yaml"),
        )


@pytest.mark.parametrize(
    "setting",
    [
        "postgresql.encryptionAtRest.provider=Unknown",
        "postgresql.encryptionAtRest.kmsKeyName=",
        "postgresql.encryptionAtRest.kmsKeyName=raw-key-material",
        "postgresql.encryptionAtRest.kmsKeyName=projects/keys/locations/global/keyRings/ring/cryptoKeys/key",
        "postgresql.encryptionAtRest.provider=ExistingStorageClass",
        "postgresql.enabled=false",
        "postgresql.encryptionAtRest.storageClass=INVALID_CLASS",
        "postgresql.encryptionAtRest.storageClass=invalid..class",
    ],
)
def test_incomplete_or_invalid_encryption_configuration_fails_before_install(setting):
    """
    Reject missing keys, unusable class names and encryption configured without a managed database.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(setting, values_files=(CHART / "values-postgresql-encryption.reference.yaml",))


def test_external_database_encryption_is_not_claimed_by_the_chart():
    """
    A DSN cannot make the chart provision or verify an external database's encrypted volumes.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(
            "postgresql.managed=false",
            "postgresql.existingSecret=external-dsn",
            values_files=(CHART / "values-postgresql-encryption.reference.yaml",),
        )


def test_disabled_encryption_preserves_existing_storage_selection():
    """
    Existing installations keep their chosen storage and acquire no encryption resources by default.
    """
    objects = render("postgresql.enabled=true", "postgresql.storage.storageClass=custom-storage")
    assert not any(obj["kind"] == "StorageClass" for obj in objects)
    assert next(obj for obj in objects if obj["kind"] == "Cluster")["spec"]["storage"]["storageClass"] == "custom-storage"


@pytest.mark.parametrize(("field", "value"), [("enabled", "true"), ("storageClass", 5), ("kmsKeyName", False), ("diskType", 1)])
def test_encryption_values_reject_wrong_scalar_types(field, value):
    """
    Helm and the packaged schema expose the same strict administrator-facing field types.
    """
    defaults = yaml.safe_load((CHART / "values.yaml").read_text())
    defaults["postgresql"]["encryptionAtRest"][field] = value
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(defaults, json.loads((CHART / "values.schema.json").read_text()))
