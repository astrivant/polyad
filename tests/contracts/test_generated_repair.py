"""
Exercise real Git merges and conservative generated repair boundaries.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest

from scripts.repair.plan import MANIFEST, git, inventory, owners, path_name, prepare


def commit(root: Path) -> str:
    """
    Commit synthetic fixture files without inheriting production signing or hooks.

    Args:
        root (Path): Synthetic repository.

    Returns:
        str: Fixture commit identifier.
    """
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture",
    )
    return git(root, "rev-parse", "HEAD").decode().strip()


def fixture(root: Path) -> tuple[str, str]:
    """
    Produce a CODEOWNERS conflict alongside independent clean source edits.

    Args:
        root (Path): Temporary directory owned by this test.

    Returns:
        tuple[str, str]: PR head and target head.
    """
    git(root, "init", "--quiet")
    (root / ".github").mkdir()
    (root / MANIFEST).write_text(
        json.dumps(
            {
                "version": 1,
                "artifacts": [
                    {
                        "output": ".github/CODEOWNERS",
                        "inputs": [".github/CODEOWNERS"],
                        "renderer": "codeowners",
                        "reason": "Literal rules",
                    },
                    {
                        "output": "generated.txt",
                        "inputs": ["source.txt"],
                        "renderer": "copy",
                        "reason": "Copy",
                    },
                ],
            }
        )
    )
    (root / ".github/CODEOWNERS").write_text("# Owners\n/a @owner\n")
    (root / "source.txt").write_text("a\nb\nc\n")
    (root / "generated.txt").write_text("a\nb\nc\n")
    ancestor = commit(root)
    (root / ".github/CODEOWNERS").write_text("# Owners\n/a @owner\n/head @owner\n")
    (root / "head.txt").write_text("Candidate input is data, never executed\n")
    head = commit(root)
    git(root, "checkout", "--quiet", "--detach", ancestor)
    (root / ".github/CODEOWNERS").write_text("# Owners\n/a @owner\n/base @owner\n")
    (root / "base.txt").write_text("Independent target change\n")
    return head, commit(root)


def test_real_merge_is_deterministic_and_preserves_source(tmp_path: Path) -> None:
    """
    Verify complete source preservation and deterministic real Git proposals.

    Args:
        tmp_path (Path): Owned temporary fixture root.

    Returns:
        None: The boundary assertions pass.
    """
    head, base = fixture(tmp_path)
    before = git(tmp_path, "status", "--porcelain")
    result = prepare(tmp_path, head, base, trusted=base)
    assert result == prepare(tmp_path, head, base, trusted=base)
    assert result["status"] == "repair"
    assert result["conflicts"] == [".github/CODEOWNERS"]
    changes = result["tree"]
    assert isinstance(changes, list)
    assert {item["path"] for item in changes} == {".github/CODEOWNERS", "base.txt"}
    assert changes[0]["content"] == "# Owners\n/a @owner\n/base @owner\n/head @owner\n"
    assert git(tmp_path, "status", "--porcelain") == before
    assert git(tmp_path, "rev-parse", "HEAD").decode().strip() == base


def test_copy_uses_merged_canonical_data(tmp_path: Path) -> None:
    """
    Regenerate copies from both sides of the merged source.

    Args:
        tmp_path (Path): Owned temporary fixture root.

    Returns:
        None: The boundary assertions pass.
    """
    head, base = fixture(tmp_path)
    git(tmp_path, "checkout", "--quiet", "--detach", head)
    (tmp_path / "generated.txt").write_text("stale head\n")
    (tmp_path / "source.txt").write_text("head\nb\nc\n")
    head = commit(tmp_path)
    git(tmp_path, "checkout", "--quiet", "--detach", base)
    (tmp_path / "generated.txt").write_text("stale target\n")
    (tmp_path / "source.txt").write_text("a\nb\nbase\n")
    base = commit(tmp_path)
    result = prepare(tmp_path, head, base, trusted=base)
    changes = result["tree"]
    assert isinstance(changes, list)
    output = next(item for item in changes if item["path"] == "generated.txt")
    assert output["content"] == "head\nb\nbase\n"


@pytest.mark.parametrize("kind", ["source", "inventory", "owner", "symlink"])
def test_ambiguous_merges_never_produce_a_partial_repair(tmp_path: Path, kind: str) -> None:
    """
    Refuse every branch mutation when any source or policy conflict is ambiguous.

    Args:
        tmp_path (Path): Owned temporary fixture root.
        kind (str): Rejected boundary to exercise.

    Returns:
        None: The boundary assertions pass.
    """

    head, base = fixture(tmp_path)
    git(tmp_path, "checkout", "--quiet", "--detach", head)
    if kind == "source":
        (tmp_path / "source.txt").write_text("head\n")
    elif kind == "inventory":
        (tmp_path / MANIFEST).write_text('{"version":1,"artifacts":[]}')
    elif kind == "owner":
        (tmp_path / ".github/CODEOWNERS").write_text("# Owners\n/a @other\n")
    else:
        (tmp_path / ".github/CODEOWNERS").unlink()
        (tmp_path / ".github/CODEOWNERS").symlink_to("../source.txt")
    head = commit(tmp_path)
    if kind == "source":
        git(tmp_path, "checkout", "--quiet", "--detach", base)
        (tmp_path / "source.txt").write_text("base\n")
        base = commit(tmp_path)
    with pytest.raises(ValueError):
        prepare(tmp_path, head, base, trusted=base)


@pytest.mark.parametrize(
    "value",
    [
        "../escape",
        "/absolute",
        ".git/config",
        "a/../../b",
        "a//b",
        "a/./b",
        "",
        "a\nfoo",
        "a\\b",
    ],
)
def test_path_boundaries(value: str) -> None:
    """
    Reject paths outside the canonical artifact namespace.

    Args:
        value (str): Invalid path example.

    Returns:
        None: The boundary assertions pass.
    """
    with pytest.raises(ValueError):
        path_name(value)


@pytest.mark.parametrize(
    "line",
    [
        "* @owner",
        "/a @other",
        "/a @owner\n/a @owner",
        "/a",
        "/a/../b @owner",
        "[section]\n/a @owner",
    ],
)
def test_unknown_or_changed_policy_requires_review(line: str) -> None:
    """
    Require review for ownership syntax outside the proven merge domain.

    Args:
        line (str): Unsupported policy example.

    Returns:
        None: The boundary assertions pass.
    """
    with pytest.raises(ValueError):
        owners("/a @owner\n", line + "\n", "/a @owner\n/base @owner\n")


def test_removal_and_independent_addition() -> None:
    """
    Preserve deletions while retaining independent ownership additions.

    Returns:
        None: The boundary assertions pass.
    """
    assert owners("/a @owner\n/b @owner\n", "/a @owner\n", "/a @owner\n/b @owner\n/c @owner\n") == "/a @owner\n/c @owner\n"


def test_inventory_rejects_commands_and_automatic_globs() -> None:
    """
    Keep command execution and broad output writes out of inventories.

    Returns:
        None: The boundary assertions pass.
    """
    with pytest.raises(ValueError):
        inventory(b'{"version":1,"artifacts":[{"command":"anything"}]}')
    with pytest.raises(ValueError):
        inventory(b'{"version":1,"artifacts":[{"output":"*.json","inputs":["data"],"renderer":"copy","reason":"test"}]}')


def test_clean_merge_is_noop(tmp_path: Path) -> None:
    """
    Avoid unnecessary branch commits for clean merges.

    Args:
        tmp_path (Path): Owned temporary fixture root.

    Returns:
        None: The boundary assertions pass.
    """
    head, _ = fixture(tmp_path)
    assert prepare(tmp_path, head, head, trusted=head)["status"] == "clean"


def test_section_comments_do_not_move() -> None:
    """
    Preserve authored policy grouping by declining sectioned documents.

    Returns:
        None: Automatic repair cannot relocate an inline policy heading.
    """
    source = "/a @owner\n# Next section\n/b @owner\n"
    with pytest.raises(ValueError, match="commentary"):
        owners(source, source + "/head @owner\n", source + "/base @owner\n")


@pytest.mark.parametrize("changed", ["neither", "head", "base", "both"])
def test_stack_base_is_independent_of_trusted_policy(tmp_path: Path, changed: str) -> None:
    """
    Separate a stacked target from policy approval even when both PR branches agree.

    Args:
        tmp_path (Path): Owned Git fixture, with no executed candidate code.
        changed (str): Branches carrying an unapproved inventory change.

    Returns:
        None: Only the original trusted inventory permits repair.
    """
    head, base = fixture(tmp_path)
    trusted = git(tmp_path, "merge-base", head, base).decode().strip()
    for label, revision in (("head", head), ("base", base)):
        if changed not in {label, "both"}:
            continue
        git(tmp_path, "checkout", "--quiet", "--detach", revision)
        (tmp_path / MANIFEST).write_text('{"version":1,"artifacts":[]}')
        if label == "head":
            head = commit(tmp_path)
        else:
            base = commit(tmp_path)
    git(tmp_path, "checkout", "--quiet", "--detach", trusted)
    if changed == "neither":
        result = prepare(tmp_path, head, base, trusted=trusted)
        assert result["status"] == "repair"
        assert result["trusted"] == trusted
        assert result["base"] == base != trusted
    else:
        with pytest.raises(ValueError, match="trusted policy"):
            prepare(tmp_path, head, base, trusted=trusted)
    assert git(tmp_path, "rev-parse", "HEAD").decode().strip() == trusted
