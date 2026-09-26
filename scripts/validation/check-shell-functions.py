"""
Require descriptions and typed signatures immediately above shell functions.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

TYPE = r"[A-Za-z_][A-Za-z0-9_\[\]|?]*"
SIGNATURE = re.compile(rf"# (?:[A-Za-z_][A-Za-z0-9_]*::{TYPE} )*-> ret::{TYPE}")


def functions(node: Any) -> Iterator[dict[str, Any]]:
    """
    Locate declarations in shfmt's syntax tree, excluding strings and heredoc examples.

    Args:
        node (Any): Decoded shfmt JSON node or container.

    Yields:
        dict[str, Any]: Function declarations, including nested shell functions.
    """
    if isinstance(node, dict):
        if node.get("Type") == "FuncDecl":
            yield node
        for child in node.values():
            yield from functions(child)
    elif isinstance(node, list):
        for child in node:
            yield from functions(child)


def violations(source: str, tree: dict[str, Any]) -> list[str]:
    """
    Validate each function's header and opening line without changing its body.

    Args:
        source (str): Original shell source, preserving line positions.
        tree (dict[str, Any]): Parsed shfmt syntax tree for that source.

    Returns:
        list[str]: Line-numbered errors, or an empty list for conforming source.
    """
    lines = source.splitlines()
    errors = []
    for function in functions(tree):
        index = function["Pos"]["Line"] - 1
        name = function["Name"]["Value"]
        header = [line.strip() for line in lines[max(0, index - 3) : index]]
        valid = (
            len(header) == 3
            and header[0] == "##"
            and header[1].startswith("# ")
            and bool(header[1][2:].strip())
            and SIGNATURE.fullmatch(header[2]) is not None
        )
        if not valid:
            errors.append(f"{index + 1}: {name}: expected ##, # Description, and # arg::type -> ret::type immediately above the function")

        # Enforce the requested brace style even if shfmt accepts function/foo syntax.
        if lines[index].strip() != f"{name}() {{":
            errors.append(f"{index + 1}: {name}: expected '{name}() {{' on its own line")
    return errors


def main() -> None:
    """
    Parse shell files with the pinned formatter and report header violations.

    Returns:
        None: Exits nonzero on invalid shell syntax or documentation.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shfmt", default="shfmt", help="Path to the pinned shfmt executable.")
    parser.add_argument("files", type=Path, nargs="*")
    args = parser.parse_args()
    if not args.files:
        root = Path(__file__).resolve().parents[2]
        manifest = subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.sh", "*.bash"], cwd=root
        )
        args.files = sorted({root / name for name in manifest.decode().split("\0") if name and (root / name).is_file()})
    failed = False
    for path in args.files:
        source = path.read_text()
        result = subprocess.run([args.shfmt, "-ln", "bash", "-tojson"], input=source, capture_output=True, text=True, check=False)
        if result.returncode:
            print(f"{path}: {result.stderr.strip()}")
            failed = True
            continue
        for error in violations(source, json.loads(result.stdout)):
            print(f"{path}:{error}")
            failed = True
    raise SystemExit(int(failed))


if __name__ == "__main__":
    main()
