"""
Exercise actual offline release signatures and reject changed trust or payloads.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from scripts.release.attest import fingerprint, prepare, verify


@pytest.fixture
def signed_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    """
    Create disposable keys and synthetic packages, never using maintainer credentials.

    Args:
        tmp_path (Path): Isolated fixture storage.
        monkeypatch (pytest.MonkeyPatch): Scoped synthetic signing environment.

    Returns:
        tuple[Path, str]: Complete signed directory and independently derived key fingerprint.
    """
    if shutil.which("cosign") is None:
        pytest.skip("Cosign is required; CI installs the pinned tool")
    monkeypatch.setenv("COSIGN_PASSWORD", "synthetic-test-only")
    prefix = tmp_path / "key"
    subprocess.run(["cosign", "generate-key-pair", "--output-key-prefix", str(prefix)], check=True, capture_output=True)
    monkeypatch.setenv("COSIGN_PRIVATE_KEY", prefix.with_suffix(".key").read_text())
    trusted = fingerprint(prefix.with_suffix(".pub"))
    directory = tmp_path / "release"
    directory.mkdir()
    for name in ("polyad", "polyad_types", "polyad_schemas", "polyad_sdk", "polyad_benchmarks"):
        for suffix in ("-py3-none-any.whl", ".tar.gz"):
            (directory / f"{name}-1.2.3{suffix}").write_text(name)
    for name in ("polyad", "polyad-crds", "polyad-benchmarks"):
        (directory / f"{name}-1.2.3.tgz").write_text(name)
    prepare(directory, "v1.2.3", "a" * 40, trusted)
    return directory, trusted


@pytest.mark.parametrize("damage", ["none", "payload", "missing", "extra", "key", "source", "manifest", "symlink"])
def test_signed_release_rejects_changed_bindings(signed_release: tuple[Path, str], damage: str) -> None:
    """
    Verify downloaded bytes against independent trust and exact source bindings.

    Args:
        signed_release (tuple[Path, str]): Synthetic signed attachment set.
        damage (str): One independent corruption or a valid round trip.

    Returns:
        None: Only the intact release passes verification.
    """
    directory, trusted = signed_release
    artifact = directory / "polyad-1.2.3-py3-none-any.whl"
    source = "a" * 40
    if damage == "payload":
        artifact.write_text("changed")
    elif damage == "missing":
        artifact.unlink()
    elif damage == "extra":
        (directory / "unexpected.txt").write_text("unexpected")
    elif damage == "key":
        trusted = "b" * 64
    elif damage == "source":
        source = "b" * 40
    elif damage == "manifest":
        (directory / "SHA256SUMS").write_text("forged")
    elif damage == "symlink":
        artifact.unlink()
        artifact.symlink_to(directory / "polyad-1.2.3.tar.gz")
    if damage == "none":
        verify(directory, "v1.2.3", source, trusted)
    else:
        with pytest.raises((ValueError, subprocess.CalledProcessError)):
            verify(directory, "v1.2.3", source, trusted)
