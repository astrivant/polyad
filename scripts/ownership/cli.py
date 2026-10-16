"""
Check or explicitly rewrite CODEOWNERS using the selected repository inventory.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from scripts.ownership.compiler import compile_owners

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["main"]


def _git(root: Path, *arguments: str) -> str:
    """
    Read Git metadata without evaluating hooks or repository filesystem monitors.

    Args:
        root (Path): Explicit existing worktree.
        *arguments (str): Literal read-only Git arguments.

    Returns:
        str: Strict UTF-8 output, including NUL-separated inventories.
    """
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    # Honor the index selected by pre-commit, but do not inherit repository routing or tracing.
    if "GIT_INDEX_FILE" in os.environ:
        environment["GIT_INDEX_FILE"] = os.environ["GIT_INDEX_FILE"]
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", "-C", str(root), *arguments],
        env=environment,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return result.stdout.decode("utf-8")


def _replace(path: Path, before: bytes, after: bytes) -> None:
    """
    Replace reviewed text atomically while preserving permissions and detecting intervening edits.

    Args:
        path (Path): Existing regular CODEOWNERS file.
        before (bytes): Previously read source bytes.
        after (bytes): Compiled candidate bytes.

    Returns:
        None: The candidate replaces the file; nothing is staged or committed.
    """
    descriptor, name = tempfile.mkstemp(prefix=".codeowners-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
            stream.write(after)
            stream.flush()
            os.fsync(stream.fileno())
        if path.is_symlink() or path.read_bytes() != before:
            raise ValueError("CODEOWNERS changed during compilation; retry after reviewing the concurrent edit")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: Sequence[str] | None = None) -> int:
    """
    Compile working text or check an immutable revision without remote access.

    Args:
        argv (Sequence[str] | None): Explicit arguments, or the process argument list.

    Returns:
        int: Zero for normalized/written content, one for drift, two for invalid input or I/O failure.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--file", default=".github/CODEOWNERS", help="Repository-relative ownership file; select GitLab's path explicitly")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--write", action="store_true")
    parser.add_argument("--revision", help="Check committed text and tree together, for example HEAD at pre-push")
    parser.add_argument("--consolidate", action="store_true", help="Opt in to new directory scopes, including their future files")
    args = parser.parse_args(argv)
    try:
        root = args.repo.resolve(strict=True)
        if Path(_git(root, "rev-parse", "--show-toplevel").strip()).resolve() != root:
            raise ValueError("--repo must name the repository root, not a subdirectory")
        relative = Path(args.file)
        if relative.is_absolute() or any(part in {"..", ".git"} for part in relative.parts):
            raise ValueError("--file must stay within the worktree, outside Git metadata")
        target = root / relative
        if any(parent.is_symlink() for parent in (target, *target.parents) if parent != root and root in parent.parents):
            raise ValueError("CODEOWNERS must not be reached through a symlink")
        if args.revision:
            if args.write:
                raise ValueError("--revision supports --check only")
            revision = _git(root, "rev-parse", "--verify", "--end-of-options", args.revision + "^{commit}").strip()
            source = _git(root, "show", f"{revision}:{relative.as_posix()}")
            inventory = _git(root, "ls-tree", "-r", "--name-only", "-z", revision)
        else:
            if _git(root, "ls-files", "--unmerged"):
                raise ValueError("Resolve index conflicts before compiling ownership")
            source = target.read_bytes().decode("utf-8")
            inventory = _git(root, "ls-files", "--cached", "-z")
        paths = frozenset(inventory.rstrip("\0").split("\0")) - {""}
        result = compile_owners(source, paths, consolidate=args.consolidate)
        if result.barriers:
            print(f"Retained {len(result.barriers)} opaque lines as ordering boundaries (patterns, sections, exclusions or absent paths).")
        for scope in result.consolidated:
            print(f"Review directory ownership including future files: {scope}")
        if result.text == source:
            print("CODEOWNERS is normalized.")
            return 0
        if args.check:
            print("CODEOWNERS needs normalization; run with --write, review the diff and commit it before pushing.")
            return 1
        _replace(target, source.encode("utf-8"), result.text.encode("utf-8"))
        print("Normalized CODEOWNERS; review and stage the diff explicitly.")
        return 0
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as error:
        # Git diagnostics can contain remote or local configuration; report only the failure category.
        detail = str(error) if isinstance(error, ValueError) and not isinstance(error, UnicodeError) else type(error).__name__
        print(f"CODEOWNERS compilation failed: {detail}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
