"""
Render real chart notes and manifests without Kubernetes credentials or discovery.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from pathlib import Path
    from typing import Any

__all__ = ("render_with_notes",)


def render_with_notes(
    chart: Path, directory: Path, *options: str, release: str = "example", namespace: str = "apps"
) -> tuple[str, list[dict[str, Any]]]:
    """
    Capture NOTES with Helm template, which is offline on both Helm 3 and Helm 4.

    Args:
        chart (Path): Source chart with its already-built dependencies.
        directory (Path): Isolated test directory for the chart copy.
        *options (str): Additional Helm flags, such as values files and overrides.
        release (str): Release name exposed to the chart.
        namespace (str): Release namespace exposed to the chart.

    Returns:
        tuple[str, list[dict[str, Any]]]: Rendered notes and actual manifests, excluding the test-only capture.
    """

    # Helm 3 install dry-runs still discover cluster capabilities. Template omits
    # NOTES, so render the unchanged source through tpl in a disposable ConfigMap.
    copied = shutil.copytree(chart, directory / chart.name)
    (copied / "test-notes.txt").write_text((copied / "templates/NOTES.txt").read_text())
    (copied / "templates/test-notes.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: rendered-notes\ndata:\n"
        '  notes: |{{ tpl (.Files.Get "test-notes.txt") . | nindent 4 }}\n'
    )
    output = subprocess.check_output(
        ["helm", "template", release, str(copied), "--namespace", namespace, *options],
        text=True,
        env={**os.environ, "KUBECONFIG": str(directory / "no-kubeconfig"), "HELM_KUBEAPISERVER": "http://127.0.0.1:1"},
    )
    documents = [obj for obj in yaml.safe_load_all(output) if obj]
    capture = next(obj for obj in documents if obj["kind"] == "ConfigMap" and obj["metadata"]["name"] == "rendered-notes")
    documents.remove(capture)
    return capture["data"]["notes"], documents
