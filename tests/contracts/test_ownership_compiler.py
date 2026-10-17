"""
Check ownership invariants, opaque boundaries and committed-tree enforcement.
"""

from __future__ import annotations

import itertools
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

import pytest
from ownerloom import compile_owners
from ownerloom.cli import main


def owners(source: str, path: str) -> tuple[str, ...]:
    """
    Evaluate synthetic literal fixtures independently of the compiler representation.

    Args:
        source (str): Literal ownership rules without comments or sections.
        path (str): Relative probe path, including hypothetical future paths.

    Returns:
        tuple[str, ...]: Last matching owners, or the unowned default.
    """
    result: tuple[str, ...] = ()
    for line in source.splitlines():
        pattern, *tokens = line.split()
        base = pattern[1:].rstrip("/")
        if path.startswith(base + "/") or (not pattern.endswith("/") and path == base):
            result = tuple(tokens)
    return result


def test_grouping_and_inherited_scope_reduction() -> None:
    """
    Group sibling directories and remove an already inherited ownership entry.

    Returns:
        None: Directory order is readable and no new scope is introduced.
    """
    source = "/z/file @z\n/a/ @a\n/z/other @z\n/a/file @a\n"
    paths = frozenset({"z/file", "z/other", "a/file"})
    result = compile_owners(source, paths)
    assert result.text == "/a/ @a\n/z/file @z\n/z/other @z\n"
    assert not result.consolidated


def test_literal_normalization_laws() -> None:
    """
    Exercise order-sensitive scope combinations against an independent ownership oracle.

    Returns:
        None: All generated cases preserve ownership, input data and normalization idempotence.
    """
    patterns = ("/a/", "/a/b", "/a/b/", "/a/c", "/z")
    paths = frozenset({"a/b", "a/b/x", "a/c", "z"})
    probes = (*paths, "a/future", "a/c/new", "a/b/new", "z/new", "outside")
    for selected in itertools.permutations(patterns, 3):
        for assignment in itertools.product(("@one", "@two"), repeat=3):
            source = "".join(f"{pattern} {owner}\n" for pattern, owner in zip(selected, assignment, strict=True))
            result = compile_owners(source, paths)
            assert [owners(source, path) for path in probes] == [owners(result.text, path) for path in probes]
            assert compile_owners(result.text, paths).text == result.text
            assert paths == frozenset({"a/b", "a/b/x", "a/c", "z"})
            folded = compile_owners(source, paths, consolidate=True)
            assert [owners(source, path) for path in paths] == [owners(folded.text, path) for path in paths]
            assert compile_owners(folded.text, paths, consolidate=True).text == folded.text


@pytest.mark.parametrize(
    "barrier", ["*.py @python", "[Docs][2] @docs", "^[Docs]", "!/secret/", "/a/", "/missing @future", "/a/b @one # note", r"/a\ b @one"]
)
def test_host_syntax_and_absent_scopes_are_barriers(barrier: str) -> None:
    """
    Preserve syntax outside the literal compiler instead of guessing host-specific behavior.

    Args:
        barrier (str): Wildcard, section, exemption, inline comment, escape or absent path.

    Returns:
        None: The opaque line stays byte-for-byte in its original boundary position.
    """
    source = f"/z @one\n{barrier}\n/a/b @two\n"
    result = compile_owners(source, frozenset({"z", "a/b"}))
    assert result.text == source
    assert result.barriers == (2,)


@pytest.mark.parametrize("source", ["", "# note\n\n", "/z @one\r\n/a @two\r\n", "/z @one\n/a @two"])
def test_line_endings_and_empty_documents(source: str) -> None:
    """
    Retain comment blocks, line-ending style and the original final-newline policy.

    Args:
        source (str): Small empty, CRLF or unterminated document.

    Returns:
        None: Normalization remains deterministic without adding a final newline.
    """
    result = compile_owners(source, frozenset({"a", "z"}))
    assert result.text.endswith("\n") == source.endswith("\n")
    assert compile_owners(result.text, frozenset({"a", "z"})).text == result.text
    if "\r\n" in source:
        assert result.text.count("\r\n") == source.count("\r\n")


def test_consolidation_is_explicit_and_requires_complete_uniform_coverage() -> None:
    """
    Fold reviewed complete subtrees while retaining gaps and distinct owners.

    Returns:
        None: New directory coverage appears only under explicit opt-in.
    """
    source = "/pkg/a @one\n/pkg/b @one\n"
    paths = frozenset({"pkg/a", "pkg/b"})
    assert compile_owners(source, paths).text == source
    result = compile_owners(source, paths, consolidate=True)
    assert result.text == "/pkg/ @one\n"
    assert result.consolidated == ("/pkg/",)
    assert all(owners(source, path) == owners(result.text, path) for path in paths)
    assert compile_owners(source, paths | {"pkg/unowned"}, consolidate=True).text == source
    assert compile_owners(source.replace("b @one", "b @two"), paths, consolidate=True).text != result.text


def test_consolidation_cannot_cross_comment_or_override() -> None:
    """
    Respect author grouping and parent overrides during optional directory folding.

    Returns:
        None: Distinct policy boundaries survive the simplification pass.
    """
    paths = frozenset({"pkg/a", "pkg/b"})
    source = "/pkg/a @one\n# boundary\n/pkg/b @one\n"
    assert compile_owners(source, paths, consolidate=True).text == source
    source = "/pkg/ @other\n/pkg/a @one\n/pkg/b @one\n"
    assert compile_owners(source, paths, consolidate=True).text == source


def git(root: Path, *args: str) -> str:
    """
    Run Git only within a synthetic repository without user hooks or signatures.

    Args:
        root (Path): Temporary test worktree.
        *args (str): Fixture commands.

    Returns:
        str: Captured Git output.
    """
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_cli_checks_committed_bytes_and_never_stages(tmp_path: Path) -> None:
    """
    Prevent an uncommitted formatting fix from masking stale ownership at pre-push.

    Args:
        tmp_path (Path): Pytest-owned synthetic repository.

    Returns:
        None: HEAD remains rejected until its normalized ownership is committed.
    """
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Fixture")
    git(tmp_path, "config", "user.email", "fixture@example.invalid")
    for name in ("a", "z"):
        (tmp_path / name).write_text(name)
    ownership = tmp_path / "CODEOWNERS"
    ownership.write_text("/z @one\n/a @two\n")
    git(tmp_path, "add", "a", "z", "CODEOWNERS")
    git(tmp_path, "commit", "-m", "Synthetic baseline")
    common = ["--repo", str(tmp_path), "--file", "CODEOWNERS"]
    assert main([*common, "--check"]) == 1
    assert main([*common, "--write"]) == 0
    assert main([*common, "--check"]) == 0
    assert main([*common, "--check", "--revision", "HEAD"]) == 1
    assert not git(tmp_path, "diff", "--cached")
    assert main([*common, "--write", "--revision", "HEAD"]) == 2
    git(tmp_path, "add", "CODEOWNERS")
    git(tmp_path, "commit", "-m", "Normalize fixture")
    assert main([*common, "--check", "--revision", "HEAD"]) == 0
    child = tmp_path / "nested"
    child.mkdir()
    assert main(["--repo", str(child), "--file", "CODEOWNERS", "--check"]) == 2
    ownership.unlink()
    ownership.symlink_to(tmp_path / "a")
    assert main([*common, "--write"]) == 2
