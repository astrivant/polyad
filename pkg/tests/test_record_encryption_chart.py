"""
Keep optional record keys restricted to operator database writers across deployment architectures.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.test_chart import CHART, render
from tests.test_chart import pytestmark as pytestmark


@pytest.mark.parametrize("profile", [None, "values-ha.reference.yaml", "values-components.reference.yaml"])
def test_record_keys_reach_operator_components_but_never_postgresql(profile):
    """
    The shared operator template projects only public key data by default in dense and split deployments.
    """
    values = ((CHART / "references" / profile,) if profile else ()) + (
        CHART / "references" / "values-postgresql-record-encryption.reference.yaml",
    )
    objects = render(values_files=values)
    pods = [obj["spec"]["template"]["spec"] for obj in objects if obj["kind"] == "Daemon"]
    pods += [obj["spec"]["template"]["spec"] for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "test-polyad"]
    assert len(pods) == (4 if profile == "values-components.reference.yaml" else 1)
    for pod in pods:
        volume = next(item for item in pod["volumes"] if item["name"] == "record-encryption")
        assert volume["secret"] == {
            "secretName": "polyad-record-keys",
            "defaultMode": 0o440,
            "items": [{"key": "public.pem", "path": "public.pem"}],
        }
        operator = next(item for item in pod["containers"] if item["name"] == "operator")
        env = {item["name"]: item.get("value") for item in operator["env"]}
        assert env["POLYAD_POSTGRES_RECORD_ENCRYPTION_ENABLED"] == "true"
        assert env["POLYAD_POSTGRES_RECORD_PUBLIC_KEY_FILE"].endswith("/public.pem")
        assert "POLYAD_POSTGRES_RECORD_PRIVATE_KEY_FILE" not in env
        assert next(item for item in operator["volumeMounts"] if item["name"] == "record-encryption")["readOnly"]
    assert "polyad-record-keys" not in str([obj for obj in objects if obj["kind"] in {"Cluster", "Secret", "ConfigMap"}])


def test_pair_and_password_reach_authentication_writers_including_observer():
    """
    Authentication-only storage may use an external database and encrypted private PEMs.
    """
    objects = render(
        "postgresql.enabled=false",
        "authentication.storage.enabled=true",
        "authentication.storage.managed=false",
        "authentication.storage.existingSecret=external-auth-dsn",
        "postgresql.recordEncryption.publicKeyKey=key.pub",
        "postgresql.recordEncryption.privateKeyKey=key.pem",
        "postgresql.recordEncryption.privateKeyPasswordKey=password",
        "observer.enabled=true",
        "global.multiCluster.clusterName=west",
        values_files=(
            CHART / "references" / "values-authentication.reference.yaml",
            CHART / "references" / "values-postgresql-record-encryption.reference.yaml",
        ),
    )
    for name in ("test-polyad", "test-polyad-observer"):
        pod = next(obj for obj in objects if obj["kind"] == "Deployment" and obj["metadata"]["name"] == name)["spec"]["template"]["spec"]
        assert next(item for item in pod["volumes"] if item["name"] == "record-encryption")["secret"]["items"] == [
            {"key": "key.pub", "path": "public.pem"},
            {"key": "key.pem", "path": "private.pem"},
            {"key": "password", "path": "password"},
        ]
        env = {item["name"]: item.get("value") for item in pod["containers"][0]["env"]}
        assert env["POLYAD_POSTGRES_RECORD_PRIVATE_KEY_FILE"].endswith("/private.pem")
        assert env["POLYAD_POSTGRES_RECORD_PRIVATE_KEY_PASSWORD_FILE"].endswith("/password")
    assert not any(obj["kind"] in {"Cluster", "StorageClass"} for obj in objects)


@pytest.mark.parametrize(
    "setting",
    [
        "postgresql.recordEncryption.existingSecret=",
        "postgresql.recordEncryption.privateKeyPasswordKey=password",
        "postgresql.recordEncryption.publicKeyKey=../key",
        "postgresql.enabled=false",
        "postgresql.recordEncryption.privateKeyKey=true",
    ],
)
def test_invalid_record_encryption_values_reject_install(setting):
    """
    Require keys, correct scalar types and at least one database writer before enabling.
    """
    with pytest.raises(subprocess.CalledProcessError):
        render(setting, values_files=(CHART / "references" / "values-postgresql-record-encryption.reference.yaml",))


def test_disabled_encryption_mounts_no_keys_and_requires_no_secret():
    """
    Default database installations need no application encryption configuration.
    """
    objects = render("postgresql.enabled=true")
    assert "record-encryption" not in str(objects)
    assert "POLYAD_POSTGRES_RECORD_" not in str(objects)
