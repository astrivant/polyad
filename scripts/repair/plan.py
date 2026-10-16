"""
Prepare bounded generated-file conflict repairs without executing candidate code.
"""

from __future__ import annotations

import argparse
import base64
import fnmatch
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

MANIFEST = ".github/generated-artifacts.json"
LIMIT = 2 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{40}")


def path_name(value: object) -> str:
    """
    Accept portable repository paths, never Git metadata or traversal.

    Args:
        value (object): Decoded inventory path.

    Returns:
        str: Validated path or pattern.

    Raises:
        ValueError: The path is not canonical and repository relative.
    """
    if not isinstance(value, str) or not value or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError("Invalid artifact path")
    path = PurePosixPath(value)
    if value == "." or path.is_absolute() or str(path) != value or any(part in {"..", ".git"} for part in path.parts):
        raise ValueError("Artifact paths must stay outside Git metadata and inside the repository")
    return value


def inventory(raw: bytes) -> list[dict[str, object]]:
    """
    Validate the closed inventory and reject executable command configuration.

    Args:
        raw (bytes): Trusted base inventory bytes.

    Returns:
        list[dict[str, object]]: Ordered artifact records.

    Raises:
        ValueError: An entry is ambiguous or unsupported.
    """
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"version", "artifacts"} or value["version"] != 1:
        raise ValueError("Expected generated-artifacts inventory version 1")
    entries = value["artifacts"]
    if not isinstance(entries, list) or len(entries) > 1000:
        raise ValueError("Expected a bounded artifact list")
    outputs: set[str] = set()
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "output",
            "inputs",
            "renderer",
            "reason",
        }:
            raise ValueError("Expected output, inputs, renderer and reason")
        output = path_name(entry["output"])
        inputs = entry["inputs"]
        renderer = entry["renderer"]
        if output in outputs or renderer not in {"codeowners", "copy", "manual"}:
            raise ValueError("Duplicate output or unsupported renderer")
        if not isinstance(inputs, list) or not inputs or not isinstance(entry["reason"], str) or not entry["reason"]:
            raise ValueError("Declare inputs and the repair/review rationale")
        for item in inputs:
            path_name(item)
        if renderer != "manual" and any(char in output for char in "*?["):
            raise ValueError("Automatic outputs must be exact paths")
        if renderer == "copy" and (len(inputs) != 1 or inputs[0] == output or any(char in inputs[0] for char in "*?[")):
            raise ValueError("Copy repair needs one distinct literal input")
        if renderer == "codeowners" and (output != ".github/CODEOWNERS" or inputs != [output]):
            raise ValueError("Ownership repair has one explicit policy path")
        outputs.add(output)
        result.append(entry)
    return result


def owners(ancestor: str, head: str, base: str) -> str:
    """
    Merge independent literal declarations with one unchanged owner group.

    Args:
        ancestor (str): Common ancestor policy.
        head (str): PR policy.
        base (str): Target branch policy.

    Returns:
        str: Canonical policy preserving independent additions and removals.

    Raises:
        ValueError: Owners, comments, duplicate patterns or syntax need human review.
    """
    documents: list[dict[str, str]] = []
    comments: list[list[str]] = []
    groups: set[str] = set()
    for text in (ancestor, head, base):
        rules: dict[str, str] = {}
        notes = []
        for line in text.splitlines():
            if not line or line.startswith("#"):
                if rules:
                    raise ValueError("Ownership section commentary needs review")
                notes.append(line)
                continue
            parts = line.split()
            if len(parts) < 2 or not re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+/?", parts[0]):
                raise ValueError("Ownership syntax needs review")
            path_name(parts[0].removeprefix("/")) if not parts[0].endswith("/") else path_name(parts[0][1:-1])
            if parts[0] in rules or not all(re.fullmatch(r"@[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?", part) for part in parts[1:]):
                raise ValueError("Duplicate patterns or unsupported owners need review")
            rules[parts[0]] = " ".join(parts[1:])
            groups.add(rules[parts[0]])
        documents.append(rules)
        comments.append(notes)
    # A formatter cannot infer changed ownership intent or comment placement.
    if len(groups) != 1 or comments[0] != comments[1] or comments[0] != comments[2]:
        raise ValueError("Changed ownership groups or commentary need review")
    old, ours, theirs = documents
    merged = {}
    for path in sorted(
        old.keys() | ours.keys() | theirs.keys(),
        key=lambda item: tuple(item.strip("/").split("/")),
    ):
        before, left, right = old.get(path), ours.get(path), theirs.get(path)
        if left == right:
            selected = left
        elif left == before:
            selected = right
        elif right == before:
            selected = left
        else:
            raise ValueError("Conflicting ownership declaration")
        if selected is not None:
            merged[path] = selected
    # Every accepted rule has the same owners, so ancestor scopes subsume children.
    lines = list(comments[0])
    for path, group in merged.items():
        if not any(path.startswith(parent.rstrip("/") + "/") for parent in merged if parent != path):
            lines.append(f"{path} {group}")
    return "\n".join(lines) + "\n"


def git(root: Path, *args: str, data: bytes | None = None, allowed: tuple[int, ...] = (0,)) -> bytes:
    """
    Run Git plumbing without inherited hooks, credentials, filters or routing.

    Args:
        root (Path): Trusted repository or isolated bare object database.
        *args (str): Literal Git arguments.
        data (bytes | None): Optional stdin payload.
        allowed (tuple[int, ...]): Expected Git exit statuses.

    Returns:
        bytes: Captured command output.

    Raises:
        ValueError: Git cannot construct or inspect the candidate.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(root),
            *args,
        ],
        input=data,
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )
    if result.returncode not in allowed:
        raise ValueError("Git candidate preparation failed")
    return result.stdout


def blob(root: Path, revision: str, path: str) -> bytes:
    """
    Read bounded regular blobs without materializing symlinks or submodules.

    Args:
        root (Path): Bare object database.
        revision (str): Validated commit or tree identifier.
        path (str): Literal repository path.

    Returns:
        bytes: Exact source bytes.

    Raises:
        ValueError: The file is missing, oversized or not regular.
    """
    row = git(root, "ls-tree", revision, "--", path).decode().split()
    if len(row) != 4 or row[0] != "100644" or row[1] != "blob":
        raise ValueError(f"Expected regular non-executable input: {path}")
    if int(git(root, "cat-file", "-s", row[2])) > LIMIT:
        raise ValueError(f"Input exceeds repair byte budget: {path}")
    return git(root, "cat-file", "blob", row[2])


def prepare(repository: Path, head: str, base: str) -> dict[str, object]:
    """
    Construct a merge tree and repair only declared, supported conflict outputs.

    Args:
        repository (Path): Existing trusted checkout with fetched immutable refs.
        head (str): Exact PR head commit.
        base (str): Exact target commit, also the trusted tooling revision.

    Returns:
        dict[str, object]: Bounded API tree updates and source bindings, or a clean no-op.

    Raises:
        ValueError: A conflict, policy change or output exceeds the approved boundary.
    """
    if not SHA.fullmatch(head) or not SHA.fullmatch(base):
        raise ValueError("Use full immutable commit identifiers")
    with tempfile.TemporaryDirectory(prefix="generated-repair-") as directory:
        root = Path(directory)
        git(root, "init", "--bare", "--quiet")
        git(root, "fetch", "--quiet", "--no-tags", str(repository.resolve()), head, base)
        trusted = blob(root, base, MANIFEST)
        if blob(root, head, MANIFEST) != trusted:
            raise ValueError("Inventory changed in the PR; review it before enabling repairs")
        entries = inventory(trusted)
        merged = git(
            root,
            "merge-tree",
            "--write-tree",
            "--name-only",
            "-z",
            head,
            base,
            allowed=(0, 1),
        ).split(b"\0")
        tree = merged[0].decode()
        conflicts = [part.decode() for part in merged[1 : merged.index(b"")]] if b"" in merged else []
        if not conflicts:
            return {"status": "clean", "head": head, "base": base}
        if len(conflicts) > 50 or not SHA.fullmatch(tree):
            raise ValueError("Merge exceeds the bounded repair domain")
        ancestor = git(root, "merge-base", "--all", head, base).decode().strip()
        if not SHA.fullmatch(ancestor):
            raise ValueError("Multiple merge bases require review")
        updates: list[dict[str, object]] = []
        for path in sorted(conflicts):
            matches = [entry for entry in entries if fnmatch.fnmatchcase(path, str(entry["output"]))]
            if len(matches) != 1 or matches[0]["renderer"] == "manual":
                raise ValueError(f"Unresolved source or undeclared conflict: {path}")
            entry = matches[0]
            if entry["renderer"] == "codeowners":
                output = owners(*(blob(root, ref, path).decode() for ref in (ancestor, head, base))).encode()
            else:
                inputs = entry["inputs"]
                assert isinstance(inputs, list)
                source = str(inputs[0])
                if source in conflicts:
                    raise ValueError(f"Canonical input also conflicts: {source}")
                output = blob(root, tree, source)
            # Reject mode changes and deletion conflicts rather than reviving files implicitly.
            blob(root, head, path)
            blob(root, base, path)
            updates.append(
                {
                    "path": path,
                    "mode": "100644",
                    "type": "blob",
                    "content": output.decode(),
                }
            )
        # Return the clean merge changes as well: the published commit has both original parents.
        repairs = {str(item["path"]): item for item in updates}
        changed = git(root, "diff-tree", "-r", "--no-renames", "--name-only", "-z", head, tree).decode().split("\0")
        for path in filter(None, changed):
            path_name(path)
            if path in repairs:
                continue
            row = git(root, "ls-tree", tree, "--", path).decode().split()
            if not row:
                updates.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            if row[0] not in {"100644", "100755"} or row[1] != "blob":
                raise ValueError("Changed symlinks or submodules require review")
            # Content can be binary. The publisher creates Git blobs using base64.
            if int(git(root, "cat-file", "-s", row[2])) > LIMIT:
                raise ValueError("Clean merge delta exceeds the repair byte budget")
            updates.append(
                {
                    "path": path,
                    "mode": row[0],
                    "type": "blob",
                    "data": base64.b64encode(git(root, "cat-file", "blob", row[2])).decode(),
                }
            )
        result: dict[str, object] = {
            "status": "repair",
            "head": head,
            "base": base,
            "conflicts": sorted(conflicts),
            "tree": updates,
        }
        if len(json.dumps(result)) > LIMIT or len(updates) > 500:
            raise ValueError("Repair candidate exceeds publication budget")
        return result


def main(argv: Sequence[str] | None = None) -> int:
    """
    Validate inventories or write a deterministic proposal without remote mutations.

    Args:
        argv (Sequence[str] | None): Explicit CLI arguments or process arguments.

    Returns:
        int: Zero for a valid result; two for a visible review-required diagnostic.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--head")
    parser.add_argument("--base")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.check:
            inventory((args.repo / MANIFEST).read_bytes())
        else:
            if not args.head or not args.base or not args.output:
                raise ValueError("Preparation requires --head, --base and --output")
            result = prepare(args.repo, args.head, args.base)
            args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        parser.exit(2, f"Generated repair requires review: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
