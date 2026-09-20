"""
Encrypt PostgreSQL JSON records with administrator-owned RSA keys and authenticated envelopes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import TYPE_CHECKING

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from polyad.operator.lifecycle.health import credential_token

if TYPE_CHECKING:
    from typing import Any

__all__ = (
    "ALGORITHM",
    "FORMAT",
    "RecordCipher",
)


FORMAT = "polyad-encrypted-record"
ALGORITHM = "RSA-OAEP-256+A256GCM"


def _context(table: str, identity: tuple[str, ...], key_id: str) -> bytes:
    """
    Bind ciphertext to its format, key and database record identity.

    Args:
        table (str): Code-owned SQL table name.
        identity (tuple[str, ...]): Ordered record identifiers, including control-plane scope.
        key_id (str): Public-key fingerprint carried by the envelope.

    Returns:
        bytes: Unambiguous authenticated context, reconstructed during decryption.
    """
    return json.dumps([FORMAT, 1, ALGORITHM, key_id, table, *identity], separators=(",", ":")).encode()


class RecordCipher:
    """
    Wrap a fresh AES-256 key per record with RSA-OAEP and authenticate its row context.
    """

    def __init__(self, public_pem: bytes, private_pem: bytes | None = None, password: bytes | None = None) -> None:
        """
        Validate the public key and any optional matching private key before writes begin.

        Args:
            public_pem (bytes): PEM RSA public key, at least 2048 bits.
            private_pem (bytes | None): Optional PEM RSA private key for local decryption.
            password (bytes | None): Optional password for an encrypted private PEM.
        """
        try:
            public = serialization.load_pem_public_key(public_pem)
            if not isinstance(public, rsa.RSAPublicKey) or public.key_size < 2048:
                raise ValueError("unsupported public key")
            private = serialization.load_pem_private_key(private_pem, password=password) if private_pem else None
            if password is not None and private is None:
                raise ValueError("password without private key")
            if private is not None and (
                not isinstance(private, rsa.RSAPrivateKey) or private.public_key().public_numbers() != public.public_numbers()
            ):
                raise ValueError("mismatched private key")
        except (TypeError, ValueError):
            raise ValueError("record encryption requires a valid RSA public key and, if supplied, its matching private key") from None
        self.public = public
        self.private = private
        self.key_id = hashlib.sha256(
            public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        ).hexdigest()

    @classmethod
    def from_environment(cls) -> RecordCipher:
        """
        Load projected Secret files and register their fingerprints with credential health checks.

        Returns:
            RecordCipher: Configured cipher; absent or malformed keys never fall back to plaintext.
        """
        if not os.environ.get("POLYAD_POSTGRES_RECORD_PUBLIC_KEY_FILE"):
            raise ValueError("record encryption requires a public key file")
        public = credential_token("POSTGRES_RECORD", setting="PUBLIC_KEY").encode()
        private = None
        password = None
        if os.environ.get("POLYAD_POSTGRES_RECORD_PRIVATE_KEY_FILE"):
            private = credential_token("POSTGRES_RECORD", setting="PRIVATE_KEY").encode()
            if not private:
                raise ValueError("record encryption private key file is empty")
        if os.environ.get("POLYAD_POSTGRES_RECORD_PRIVATE_KEY_PASSWORD_FILE"):
            password = credential_token("POSTGRES_RECORD", setting="PRIVATE_KEY_PASSWORD").encode()
        return cls(public, private, password)

    def encrypt(self, document: dict[str, Any], table: str, identity: tuple[str, ...]) -> dict[str, Any]:
        """
        Encrypt one JSON payload before handing parameters to the PostgreSQL driver.

        Args:
            document (dict[str, Any]): JSON-serializable application payload.
            table (str): SQL table containing the payload.
            identity (tuple[str, ...]): Stable ordered record identifiers.

        Returns:
            dict[str, Any]: Versioned JSON envelope containing ciphertext and a wrapped random data key.
        """
        key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)

        # Authenticate the row address too, so moving valid ciphertext to another row is detected.
        aad = _context(table, identity, self.key_id)
        wrapped = self.public.encrypt(
            key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=FORMAT.encode())
        )
        ciphertext = AESGCM(key).encrypt(nonce, json.dumps(document, separators=(",", ":"), allow_nan=False).encode(), aad)
        return {
            "format": FORMAT,
            "version": 1,
            "algorithm": ALGORITHM,
            "keyId": self.key_id,
            "wrappedKey": base64.b64encode(wrapped).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
        }

    def decrypt(self, envelope: dict[str, Any], table: str, identity: tuple[str, ...]) -> dict[str, Any]:
        """
        Recover a record only with its matching private key and original row context.

        Args:
            envelope (dict[str, Any]): Stored JSON envelope, never a plaintext fallback.
            table (str): Original SQL table name.
            identity (tuple[str, ...]): Original ordered record identifiers.

        Returns:
            dict[str, Any]: Authenticated, decoded JSON payload.
        """
        if self.private is None:
            raise ValueError("record decryption requires the matching private key")
        try:
            if (
                set(envelope) != {"format", "version", "algorithm", "keyId", "wrappedKey", "nonce", "ciphertext"}
                or envelope["format"] != FORMAT
                or type(envelope["version"]) is not int
                or envelope["version"] != 1
                or envelope["algorithm"] != ALGORITHM
                or envelope["keyId"] != self.key_id
            ):
                raise ValueError("invalid envelope")
            wrapped, nonce, ciphertext = (
                base64.b64decode(envelope[field], validate=True) for field in ("wrappedKey", "nonce", "ciphertext")
            )
            if len(nonce) != 12:
                raise ValueError("invalid nonce")
            key = self.private.decrypt(
                wrapped, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=FORMAT.encode())
            )
            if len(key) != 32:
                raise ValueError("invalid data key")
            document = json.loads(AESGCM(key).decrypt(nonce, ciphertext, _context(table, identity, self.key_id)))
            if not isinstance(document, dict):
                raise ValueError("invalid payload")
        except (TypeError, ValueError, InvalidTag):
            raise ValueError("record decryption failed: invalid envelope, key or row context") from None
        return document
