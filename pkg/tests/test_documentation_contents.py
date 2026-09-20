"""
Keep documentation links correct when headings repeat or generated reports change.
"""

from __future__ import annotations

import runpy
import subprocess
from pathlib import Path
from textwrap import dedent
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

TOOLS = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/documentation/update-contents.py"))
main = TOOLS["main"]
with_contents = TOOLS["with_contents"]


def test_contents_skip_examples_and_comments() -> None:
    """
    Ignore example headings and hidden generator code, preserving document content.

    Returns:
        None: Generated contents match the visible sections and remain stable.
    """
    document = dedent(
        """
        # Guide

        Introduction.

        ## Install `--filter`

        ````markdown
        ## Not a section
        ```
        ### Still inside the fence
        ````

        <!--
        ## Generator comment
        -->

        ### [Next steps](next.md) & checks

        ## Install `--filter`

        ## Install --filter-1
        """
    ).lstrip()
    result = with_contents(document)
    assert "- [Install --filter](#install---filter)" in result
    assert "  - [Next steps &amp; checks](#next-steps--checks)" in result
    assert "- [Install --filter](#install---filter-1)" in result
    assert "- [Install --filter-1](#install---filter-1-1)" in result
    contents, body = result.split("<!-- toc:end -->", 1)
    assert "Not a section" not in contents
    assert "Still inside the fence" not in contents
    assert "Generator comment" not in contents
    assert body.lstrip() == document.split("\n\n", 1)[1]
    assert with_contents(result) == result


def test_contents_update_removed_and_new_sections() -> None:
    """
    Regenerate links without keeping stale entries or accumulating contents blocks.

    Returns:
        None: Generated contents match the visible sections and remain stable.
    """
    result = with_contents("# Report\n\n## Before\n\nText.\n")
    result = with_contents(result.replace("## Before", "## After") + "\n## Details\n")
    assert "#before" not in result
    assert "[After](#after)" in result
    assert "[Details](#details)" in result
    assert result.count("<!-- toc:start -->") == 1
    assert with_contents("# Single page\n\nText.\n").count("[Single page](#single-page)") == 1
    assert with_contents("Plain text without headings.\n") == "Plain text without headings.\n"


def test_contents_keep_long_overviews_visible() -> None:
    """
    Keep every main section linked without hiding a long overview inside details.

    Returns:
        None: Generated contents match the visible sections and remain stable.
    """
    document = "# Charts\n\n" + "".join(f"## Chart {index}\n\n" for index in range(21))
    result = with_contents(document)
    contents = result.split("<!-- toc:end -->", 1)[0]
    assert "**Table of contents**" in contents
    assert "<details" not in contents and "<summary" not in contents
    assert "- [Chart 20](#chart-20)" in result
    assert with_contents(result) == result


def test_contents_limit_guides_and_reports_to_main_sections(tmp_path: Path) -> None:
    """
    Show the same shallow overview in guides and reports without removing deep body headings.

    Args:
        tmp_path (Path): Maintained guide and report locations.

    Returns:
        None: Contents include levels two and three, preserve the document body and remain stable.
    """
    document = dedent(
        """
        # Guide

        ## Testing
        ### Charts
        #### Errors
        ##### Examples
        ###### Reproduction
        """
    ).lstrip()
    guide = tmp_path / "README.md"
    report = tmp_path / "reports" / "scan.md"
    report.parent.mkdir()
    guide.write_text(document)
    report.write_text(document)
    assert main([str(guide), str(report)]) == 0
    contents, body = guide.read_text().split("<!-- toc:end -->", 1)
    assert "- [Testing](#testing)" in contents
    assert "  - [Charts](#charts)" in contents
    assert all(f"[{label}]" not in contents for label in ("Errors", "Examples", "Reproduction"))
    assert body.lstrip() == document.split("\n\n", 1)[1]
    assert report.read_text() == with_contents(document, max_depth=3)
    assert report.read_text() == guide.read_text()
    report_contents = report.read_text().split("<!-- toc:end -->", 1)[0]
    assert "[Charts](#charts)" in report_contents and "[Errors](#errors)" not in report_contents
    assert main(["--check", str(guide), str(report)]) == 0


def test_contents_preserve_anchors_allocated_by_omitted_headings() -> None:
    """
    Count every rendered heading when assigning anchors, including headings absent from contents.

    Returns:
        None: Main-section links still resolve when an earlier deeper heading has the same label.
    """
    document = "# Guide\n\n## Setup\n\n### Steps\n\n#### Repeat\n\n## Repeat\n\n"
    result = with_contents(document)
    contents = result.split("<!-- toc:end -->", 1)[0]
    assert "[Repeat](#repeat-1)" in contents
    assert "[Repeat](#repeat)" not in contents
    shallow = with_contents(document, max_depth=2).split("<!-- toc:end -->", 1)[0]
    assert "[Steps]" not in shallow
    assert "[Repeat](#repeat-1)" in shallow


def test_contents_unwrap_previous_overviews_but_preserve_collapsible_examples() -> None:
    """
    Replace the old generated wrapper without touching authored details in the document body.

    Returns:
        None: Contents stay visible, example markup stays intact and repeated updates are stable.
    """
    body = "## Setup\n\n### Steps\n\n#### Detail\n\n<details>\n<summary>Example</summary>\n\nCode.\n\n</details>\n"
    previous = (
        "# Guide\n\n<!-- toc:start -->\n<details>\n<summary>Table of contents</summary>\n\n"
        "- [Setup](#setup)\n  - [Steps](#steps)\n    - [Detail](#detail)\n\n</details>\n<!-- toc:end -->\n\n" + body
    )
    result = with_contents(previous)
    contents, remaining = result.split("<!-- toc:end -->", 1)
    assert "<details" not in contents and "<summary" not in contents
    assert "[Detail]" not in contents
    assert remaining.lstrip() == body
    assert with_contents(result) == result


def test_contents_command_check_and_archive_exclusions(tmp_path: Path) -> None:
    """
    Check without writing, update ordinary docs, and leave checksummed snapshots intact.

    Args:
        tmp_path (Path): Temporary maintained document and retained run directory.

    Returns:
        None: Checks do not write and archived snapshots remain unchanged.
    """
    document = tmp_path / "README.md"
    document.write_text("# Guide\n\n## Run\n")
    original = document.read_text()
    archived = tmp_path / "runs" / "README.md"
    archived.parent.mkdir()
    archived.write_text(original)
    assert main(["--check", str(document)]) == 1
    assert document.read_text() == original
    assert main([str(document), str(archived)]) == 0
    assert main(["--check", str(document), str(archived)]) == 0
    assert archived.read_text() == original


def test_legacy_contents_are_replaced_without_removing_prose() -> None:
    """
    Migrate editor-generated contents without duplicating links or deleting the introduction.

    Returns:
        None: Old links disappear, new links match headings, and prose remains unchanged.
    """
    document = dedent(
        """
        # Polyad

        Introduction.

        ## Table of contents

        - [Polyad](#polyad)
          - [Table of contents](#table-of-contents)
          - [Old name](#old-name)

        A note following the old contents.

        ## New name

        Text.
        """
    ).lstrip()
    result = with_contents(document)
    assert "#old-name" not in result
    assert "#table-of-contents" not in result
    assert "[New name](#new-name)" in result
    assert "Introduction." in result
    assert "A note following the old contents." in result
    assert result.count("<!-- toc:start -->") == 1
    assert with_contents(result) == result


def test_repository_inventory_updates_unstaged_docs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Discover all documentation even when a commit changes only a non-Markdown file.

    Args:
        tmp_path (Path): Isolated repository directory.
        monkeypatch (pytest.MonkeyPatch): Pytest working-directory override.

    Returns:
        None: Tracked and untracked docs update, ignored and archived files remain untouched.
    """
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    document = "# Guide\n\n## New section\n"
    (tmp_path / "README.md").write_text(document)
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/guide.md").write_text(document)
    (tmp_path / ".gitignore").write_text("ignored.md\n")
    (tmp_path / "ignored.md").write_text(document)
    (tmp_path / "runs").mkdir()
    (tmp_path / "runs/snapshot.md").write_text(document)
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
    assert "[New section](#new-section)" in (tmp_path / "README.md").read_text()
    assert "[New section](#new-section)" in (tmp_path / "docs/guide.md").read_text()
    assert (tmp_path / "ignored.md").read_text() == document
    assert (tmp_path / "runs/snapshot.md").read_text() == document
    assert main(["--check"]) == 0
