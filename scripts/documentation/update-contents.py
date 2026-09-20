"""
Maintain linked contents for repository documentation and generated Markdown reports.
"""

from __future__ import annotations

import argparse
import html
import re
import subprocess
from pathlib import Path

START = "<!-- toc:start -->"
END = "<!-- toc:end -->"


def heading_inventory(document: str) -> list[tuple[int, int, str, str]]:
    """
    Find headings and allocate matching, unique Markdown and PDF destinations.

    Args:
        document (str): Markdown source without a generated contents block.

    Returns:
        list[tuple[int, int, str, str]]: Line index, heading level, visible label, and unique anchor.
    """
    lines = document.splitlines(keepends=True)
    headings: list[tuple[int, int, str, str]] = []
    used: set[str] = set()
    fence = ""
    comment = False
    for index, line in enumerate(lines):
        if fence:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
                fence = ""
            continue
        visible = line
        if comment:
            if "-->" not in visible:
                continue
            visible = visible.split("-->", 1)[1]
            comment = False
        visible = re.sub(r"<!--.*?-->", "", visible)
        if "<!--" in visible:
            visible = visible.split("<!--", 1)[0]
            comment = True
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", visible)
        if marker:
            fence = marker[1]
            continue
        heading = re.match(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*$", visible)
        if heading is None:
            continue
        label = re.sub(r"\s+#+\s*$", "", heading[2])
        label = re.sub(r"!?\[([^\]]+)\]\([^)]*\)", r"\1", label)
        label = html.unescape(re.sub(r"<[^>]+>", "", label))
        label = re.sub(r"[`*~]", "", label)
        label = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", label)
        base = re.sub(r"[^\w\- ]", "", label.lower()).replace(" ", "-")
        anchor = base
        suffix = 0
        while anchor in used:
            suffix += 1
            anchor = f"{base}-{suffix}"
        used.add(anchor)
        headings.append((index, len(heading[1]), label, anchor))
    return headings


def with_contents(document: str, *, max_depth: int = 6) -> str:
    """
    Insert or refresh contents from ATX headings outside fenced code and HTML comments.

    Args:
        document (str): Markdown source, optionally containing our generated contents block.
        max_depth (int): Deepest heading level to include; reports can limit this to chart headings.

    Returns:
        str: Markdown with linked sections through the requested depth after its title. Long lists
            are collapsible; pages without sections link to their title.
    """
    document = re.sub(r"<!-- toc:start -->.*?<!-- toc:end -->\n*", "", document, flags=re.DOTALL)
    lines = document.splitlines(keepends=True)
    for index, _, label, _ in reversed(heading_inventory(document)):
        if label.casefold() != "table of contents":
            continue
        end = index + 1
        while end < len(lines) and (not lines[end].strip() or re.match(r"^\s*[-*+] \[.*\]\(.*\)\s*$", lines[end])):
            end += 1
        del lines[index:end]
    document = "".join(lines)
    headings = heading_inventory(document)
    if not headings:
        return document
    title = headings[0] if headings[0][1] == 1 else None
    sections = [heading for heading in headings if heading != title and heading[1] <= max_depth]
    if not sections:
        sections = headings[:1]
    minimum = min(heading[1] for heading in sections)
    entries = []
    for _, level, label, anchor in sections:
        label = html.escape(label, quote=False).replace("[", "\\[").replace("]", "\\]")
        entries.append(f"{'  ' * (level - minimum)}- [{label}](#{anchor})")
    block = [START, "<details>", "<summary>Table of contents</summary>", ""] if len(entries) > 20 else [START, "**Table of contents**", ""]
    block.extend(entries)
    if len(entries) > 20:
        block.extend(["", "</details>"])
    block.extend([END, "", ""])
    position = title[0] + 1 if title else 0
    while position < len(lines) and not lines[position].strip():
        position += 1
    prefix = "".join(lines[:position])
    return prefix + ("\n" if prefix and not prefix.endswith("\n\n") else "") + "\n".join(block) + "".join(lines[position:])


def main(argv: list[str] | None = None) -> int:
    """
    Update maintained Markdown pages, or check them without writing.

    Args:
        argv (list[str] | None): Optional explicit command arguments.

    Returns:
        int: One if a check finds stale contents; otherwise zero.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report stale contents without changing files")
    parser.add_argument("files", nargs="*", type=Path, help="Markdown files; defaults to repository documentation")
    args = parser.parse_args(argv)
    paths = args.files
    if not paths:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md"],
            capture_output=True,
            check=True,
            text=True,
        )
        paths = [Path(name) for name in result.stdout.split("\0") if name]
    changed = []
    for path in sorted(set(paths)):
        if not path.is_file() or any(part in {"third_party", "runs"} or part.endswith("-runs") for part in path.parts):
            continue
        original = path.read_text()
        updated = with_contents(original, max_depth=3 if "reports" in path.parts else 6)
        if updated != original:
            changed.append(path)
            if not args.check:
                path.write_text(updated)
    for path in changed:
        print(f"{'Outdated' if args.check else 'Updated'} contents: {path}")
    return int(args.check and bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())
