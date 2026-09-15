"""
Require Python docstring delimiters on separate lines throughout the project.
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path


def violations(path: Path) -> list[int]:
    """
    Locate docstrings whose opening or closing quotes share a line with content.

    Args:
        path (Path): Python source file to inspect without importing it.

    Returns:
        list[int]: Starting line numbers of docstrings that need expansion.
    """
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    result = []
    for node in ast.walk(ast.parse(source, filename=str(path))):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if ast.get_docstring(node, clean=False) is None:
            continue
        docstring = node.body[0]
        assert docstring.end_lineno is not None
        opening = re.fullmatch(r"[ru]?(\"{3}|'{3})", lines[docstring.lineno - 1].strip(), re.IGNORECASE)
        if opening is None or lines[docstring.end_lineno - 1].strip() != opening[1]:
            result.append(docstring.lineno)
    return result


def main() -> int:
    """
    Check selected Python files or directories and report actionable locations.

    Returns:
        int: Zero when every docstring follows the layout, otherwise one.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path, help="Python files or directories to check")
    paths = parser.parse_args().paths
    files = {file for path in paths for file in (path.rglob("*.py") if path.is_dir() else [path])}
    failed = False
    for path in sorted(files):
        for line in violations(path):
            print(f"{path}:{line}: put opening and closing docstring quotes on their own lines")
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
