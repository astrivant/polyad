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


def test_contents_collapse_long_reports() -> None:
    """
    Keep chart-heavy reports navigable without pushing the summary far down the page.

    Returns:
        None: Generated contents match the visible sections and remain stable.
    """
    document = "# Charts\n\n" + "".join(f"## Chart {index}\n\n" for index in range(21))
    result = with_contents(document)
    assert "<summary>Table of contents</summary>" in result
    assert "- [Chart 20](#chart-20)" in result
    assert with_contents(result) == result


def test_contents_include_deep_sections_and_keep_reports_brief(tmp_path: Path) -> None:
    """
    Keep guide subsections discoverable while limiting scan reports to chart headings.

    Args:
        tmp_path (Path): Maintained guide and report locations.

    Returns:
        None: Guide contents cover all levels; report contents omit individual errors and remain stable.
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
    assert "    - [Errors](#errors)" in contents
    assert "      - [Examples](#examples)" in contents
    assert "        - [Reproduction](#reproduction)" in contents
    assert body.lstrip() == document.split("\n\n", 1)[1]
    assert report.read_text() == with_contents(document, max_depth=3)
    report_contents = report.read_text().split("<!-- toc:end -->", 1)[0]
    assert "[Charts](#charts)" in report_contents and "[Errors](#errors)" not in report_contents
    assert main(["--check", str(guide), str(report)]) == 0


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
