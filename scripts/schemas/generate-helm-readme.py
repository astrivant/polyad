"""
Render typed Helm parameter documentation without changing published default values.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
from pathlib import Path

import yaml


def main() -> int:
    """
    Translate type metadata into visible documentation before invoking the pinned generator.

    Returns:
        int: Generator exit status; original values files are never rewritten here.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--readme", required=True)
    parser.add_argument("--values", required=True)
    args = parser.parse_args()
    source = Path(args.values).read_text()
    values = yaml.safe_load(source)
    names = re.findall(r"^\s*##\s*@param\s+(\S+)", source, flags=re.MULTILINE)
    parents = {name for name in names if any(other.startswith(name + ".") for other in names)}
    source = (
        "\n".join(
            line
            for line in source.splitlines()
            if not ((match := re.match(r"^\s*##\s*@param\s+(\S+)", line)) and (match[1] in parents or "[]" in match[1]))
        )
        + "\n"
    )

    def parameter(match: re.Match[str]) -> str:
        tags = [value.strip() for value in match[2].split(",")]
        description = " or ".join(value for value in tags if value != "nullable")
        if "nullable" in tags:
            description += " or null"
        # Collection modifiers control flattening; their replaced defaults are
        # restored from the actual YAML after generating the table.
        modifier = next((value for value in tags if value in {"array", "object"}), None)
        if "nullable" in tags and modifier is None:
            modifier = "nullable"
        return f"{match[1]}{f'[{modifier}] ' if modifier else ''}**Type: {description}.** {match[3]}"

    rendered = re.sub(r"^(\s*##\s*@param\s+\S+\s+)\[([^]]+)\]\s*(.*)$", parameter, source, flags=re.MULTILINE)
    with tempfile.TemporaryDirectory(prefix="polyad-readme-") as directory:
        path = Path(directory) / "values.yaml"
        path.write_text(rendered)
        result = subprocess.run(["readme-generator", f"--readme={args.readme}", f"--values={path}"], check=False)
        if result.returncode:
            return result.returncode

    def default(match: re.Match[str]) -> str:
        value = values
        remaining = match[2]
        while remaining:
            key = next(key for key in sorted(value, key=len, reverse=True) if remaining == key or remaining.startswith(key + "."))
            value = value[key]
            remaining = remaining[len(key) :].removeprefix(".")
        rendered = value if isinstance(value, str) and value else json.dumps(value, ensure_ascii=False)
        rendered = rendered.replace("|", "\\|").replace("`", "&#96;").replace("\n", "\\n")
        return f"{match[1]}| `{rendered}` |"

    readme = Path(args.readme)
    output = re.sub(r"^(\|\s*`([^`]+)`\s*\|.*)\|\s*`[^`]*`\s*\|$", default, readme.read_text(), flags=re.MULTILINE)
    readme.write_text(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
