"""
Verify optional encryption, row binding and ciphertext-only PostgreSQL payloads.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from polyad.auth.store import CredentialStore
from polyad.operator.adapters.postgresql import StateStore, state_document
from polyad.sql import record_cipher
from polyad.sql.encryption import RecordCipher
from polyad_types.auth import APIKey
from polyad_types.codec import to_dict
from tests.test_operator import resource
from tests.test_runtime_capabilities import assert_absent, probe


@pytest.fixture(autouse=True)
def isolate_credential_fingerprints(monkeypatch):
    """
    Keep temporary test Secret paths out of the process-wide lifecycle after each test.
    """
    from polyad.operator.lifecycle.health import lifecycle

    monkeypatch.setattr(lifecycle, "credentials", {})


@pytest.fixture(scope="module")
def key_pair():
    """
    Generate disposable keys without placing private material in the repository.
    """
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_pem = private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    private_pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return public_pem, private_pem


@pytest.fixture
def encrypted_writer(monkeypatch, tmp_path, key_pair):
    """
    Configure writers with only the public key and retain the private key for assertions.
    """
    public, private = key_pair
    path = tmp_path / "public.pem"
    path.write_bytes(public)
    monkeypatch.setenv("POLYAD_POSTGRES_RECORD_ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("POLYAD_POSTGRES_RECORD_PUBLIC_KEY_FILE", str(path))
    monkeypatch.delenv("POLYAD_POSTGRES_RECORD_PRIVATE_KEY_FILE", raising=False)
    monkeypatch.delenv("POLYAD_POSTGRES_RECORD_PRIVATE_KEY_PASSWORD_FILE", raising=False)
    return RecordCipher(public, private)


def test_randomized_encryption_round_trips_large_records_with_public_only_writer(key_pair):
    """
    Payload size is independent of RSA limits and repeated plaintext produces different ciphertext.
    """
    public, private = key_pair
    writer, reader = RecordCipher(public), RecordCipher(public, private)
    payload = {"value": "classified" * 10000, "vertices": [1, 2, 3], "details": {"label": "🩰"}}
    identity = ("scope", "cluster", "namespace")
    first, second = (writer.encrypt(payload, "polyad_namespace_state", identity) for _ in range(2))
    assert first != second
    assert first["wrappedKey"] != second["wrappedKey"]
    assert "classified" not in json.dumps(first)
    assert reader.decrypt(first, "polyad_namespace_state", identity) == payload
    assert reader.decrypt(second, "polyad_namespace_state", identity) == payload
    with pytest.raises(ValueError, match="private key"):
        writer.decrypt(first, "polyad_namespace_state", identity)


@pytest.mark.parametrize("field", ["version", "algorithm", "keyId", "wrappedKey", "nonce", "ciphertext"])
def test_modified_envelope_is_rejected(key_pair, field):
    """
    No damaged or substituted field may be accepted as plaintext or silently repaired.
    """
    cipher = RecordCipher(*key_pair)
    record = cipher.encrypt({"answer": 42}, "table", ("scope", "row"))
    record[field] = "invalid"
    with pytest.raises(ValueError, match="decryption failed"):
        cipher.decrypt(record, "table", ("scope", "row"))
    with pytest.raises(ValueError, match="decryption failed"):
        cipher.decrypt({"answer": 42}, "table", ("scope", "row"))


@pytest.mark.parametrize(("table", "identity"), [("other-table", ("scope", "row")), ("table", ("other-scope", "row"))])
def test_records_cannot_be_moved_to_another_table_or_scope(key_pair, table, identity):
    """
    Authenticated row context prevents otherwise valid ciphertext from being substituted elsewhere.
    """
    cipher = RecordCipher(*key_pair)
    record = cipher.encrypt({"secret": "value"}, "table", ("scope", "row"))
    with pytest.raises(ValueError, match="decryption failed"):
        cipher.decrypt(record, table, identity)


def test_wrong_or_mismatched_keys_fail_and_old_keys_still_read_old_rows(key_pair):
    """
    Rotation identifies the encryption key without losing decryption using the retained old pair.
    """
    public, private = key_pair
    replacement = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_private = replacement.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    other_public = replacement.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    old, new = RecordCipher(public, private), RecordCipher(other_public, other_private)
    record = old.encrypt({"secret": 42}, "table", ("scope",))
    assert new.key_id != old.key_id
    with pytest.raises(ValueError, match="matching private key"):
        RecordCipher(public, other_private)
    with pytest.raises(ValueError, match="decryption failed"):
        new.decrypt(record, "table", ("scope",))
    assert old.decrypt(record, "table", ("scope",)) == {"secret": 42}


def test_encrypted_private_pem_and_missing_or_weak_keys(key_pair):
    """
    Accept password-protected matching PEMs while rejecting invalid encryption configuration.
    """
    public, private = key_pair
    key = serialization.load_pem_private_key(private, None)
    protected = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(b"secret")
    )
    cipher = RecordCipher(public, protected, b"secret")
    assert cipher.decrypt(cipher.encrypt({}, "table", ()), "table", ()) == {}
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024).public_key()
    elliptic = ec.generate_private_key(ec.SECP256R1()).public_key()
    for unsupported in (weak, elliptic):
        with pytest.raises(ValueError, match="valid RSA"):
            RecordCipher(unsupported.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    for arguments in ((b"invalid",), (public, protected), (public, protected, b"wrong"), (public, None, b"secret")):
        with pytest.raises(ValueError, match="valid RSA"):
            RecordCipher(*arguments)


def test_enabled_writers_fail_closed_before_opening_a_database(monkeypatch, tmp_path):
    """
    Enablement without valid key files must never open a pool and continue unencrypted.
    """
    monkeypatch.setenv("POLYAD_POSTGRES_RECORD_ENCRYPTION_ENABLED", "true")
    monkeypatch.delenv("POLYAD_POSTGRES_RECORD_PUBLIC_KEY_FILE", raising=False)
    with patch("polyad.auth.store.ConnectionPool") as pool:
        with pytest.raises(ValueError, match="public key file"):
            CredentialStore("postgresql://unused", "scope")
        pool.assert_not_called()
    missing = tmp_path / "missing.pem"
    monkeypatch.setenv("POLYAD_POSTGRES_RECORD_PUBLIC_KEY_FILE", str(missing))
    with pytest.raises(FileNotFoundError):
        StateStore("postgresql://unused", "scope")
    missing.write_text("invalid PEM")
    with pytest.raises(ValueError, match="valid RSA"):
        StateStore("postgresql://unused", "scope")
    monkeypatch.setenv("POLYAD_POSTGRES_RECORD_ENCRYPTION_ENABLED", "typo")
    with pytest.raises(ValueError, match="true or false"):
        record_cipher()


def test_private_files_and_rotation_are_tracked(encrypted_writer, monkeypatch, tmp_path, key_pair):
    """
    A mounted pair supports decryption and participates in the existing credential health checks.
    """
    from polyad.operator.lifecycle.health import lifecycle

    private = tmp_path / "private.pem"
    private.write_bytes(key_pair[1])
    monkeypatch.setenv("POLYAD_POSTGRES_RECORD_PRIVATE_KEY_FILE", str(private))
    cipher = record_cipher()
    assert cipher.private is not None
    assert lifecycle.credentials[private] == hashlib.sha256(key_pair[1]).digest()
    assert cipher.decrypt(cipher.encrypt({}, "table", ()), "table", ()) == {}


def test_disabled_database_encryption_does_not_import_cryptography():
    """
    Installing the production dependency must not activate it for ordinary database writers.
    """
    result = probe(
        {"POLYAD_POSTGRES_ENABLED": "true", "POLYAD_POSTGRES_DSN": "postgresql://unused"},
        code="import json, sys; from polyad.operator.adapters.postgresql import StateStore; "
        "s = StateStore.from_environment('test'); assert s.cipher is None; print(json.dumps(sorted(sys.modules)))",
    )
    assert_absent(result, "cryptography", "polyad.sql.encryption")


def test_state_and_event_writes_only_send_envelopes_to_the_driver(encrypted_writer):
    """
    Check all three state payload columns and preserve deterministic event deduplication identifiers.
    """

    async def scenario():
        store = StateStore("postgresql://unused", "scope")
        store.start = AsyncMock()
        connection = AsyncMock()
        cursor = AsyncMock()
        connection.cursor = MagicMock()
        connection.cursor.return_value.__aenter__ = AsyncMock(return_value=cursor)
        pool = MagicMock()
        pool.connection.return_value.__aenter__ = AsyncMock(return_value=connection)
        pool.connection.return_value.__aexit__ = AsyncMock(return_value=None)
        store.pool = pool
        graph = resource("Graph", "pipeline", {"description": "classified", "nodes": []})
        snapshot = {"parameters": {"classified": 42}}
        assert await store.save("west", "apps", "now", [graph], snapshot)
        namespace_row = connection.execute.call_args_list[0].args[1]
        assert encrypted_writer.decrypt(namespace_row[-1].obj, "polyad_namespace_state", namespace_row[:3]) == snapshot
        graph_row = cursor.executemany.call_args.args[1][0]
        assert encrypted_writer.decrypt(graph_row[-1].obj, "polyad_graph_state", graph_row[:7]) == state_document(graph)
        assert "classified" not in json.dumps(namespace_row[-1].obj)
        assert "classified" not in json.dumps(graph_row[-1].obj)
        event = {"message": "classified"}
        await store.record_event(event)
        first = connection.execute.call_args_list[-2].args[1]
        await store.record_event(event)
        second = connection.execute.call_args_list[-2].args[1]
        assert first[:2] == second[:2]
        assert first[-1].obj != second[-1].obj
        assert encrypted_writer.decrypt(first[-1].obj, "polyad_event_history", first[:2]) == event

    asyncio.run(scenario())


def test_authentication_policy_is_encrypted_and_revocation_still_blocks(encrypted_writer):
    """
    Keep token verifiers and lane checks usable without sending plaintext policy JSON to SQL.
    """
    with patch("polyad.auth.store.ConnectionPool") as pool:
        store = CredentialStore("postgresql://unused", "scope")
        connection = pool.return_value.connection.return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = (False,)
        key = APIKey(name="client", direction="Inbound", existingSecret="classified", endpoints=("events",))
        assert store.permitted("services", key, "bearer-secret")
        row = connection.execute.call_args.args[1]
        assert row[3] == hashlib.sha256(b"bearer-secret").hexdigest()
        assert "classified" not in json.dumps(row[-1].obj)
        assert encrypted_writer.decrypt(row[-1].obj, "polyad_auth_keys", row[:5]) == to_dict(key)
        connection.reset_mock()
        connection.execute.return_value.fetchone.return_value = (True,)
        assert not store.permitted("services", key, "bearer-secret")
        assert connection.execute.call_count == 2
