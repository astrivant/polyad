"""
Check the public typing contract with Mypy, including an installed wheel in CI.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check_types(path):
    """
    Run Mypy against the configured consumer interpreter without local type caches.
    """
    environment = os.environ.copy()
    environment.pop("MYPYPATH", None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--config-file",
            str(ROOT / "pyproject.toml"),
            "--python-executable",
            os.environ.get("POLYAD_TYPING_PYTHON", sys.executable),
            "--no-incremental",
            "--cache-dir=/dev/null",
            str(path),
        ],
        cwd=path.parent,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_public_generic_types(tmp_path):
    """
    Preserve inference, specialization, covariance and default types for consumers.
    """
    consumer = tmp_path / "consumer.py"
    consumer.write_text((ROOT / "examples/typed_graphs.py").read_text())
    result = check_types(consumer)
    assert result.returncode == 0, result.stdout + result.stderr


def test_invalid_graph_types_are_rejected(tmp_path):
    """
    Reject leaf-node parameters and references outside a graph's declared specialization.
    """
    consumer = tmp_path / "invalid.py"
    consumer.write_text(
        "from attrs import frozen\n"
        "from polyad.graph import GraphNode, Node, PolyGraph\n"
        "@frozen(kw_only=True)\n"
        "class RegionalGraph(GraphNode):\n"
        "    region: str\n"
        "leaf: PolyGraph[Node]\n"
        "graph = PolyGraph[RegionalGraph](nodes=(GraphNode(name='a', kind='Graph', ref='a'),))\n"
    )
    result = check_types(consumer)
    assert result.returncode == 1, result.stdout + result.stderr
    errors = [line for line in result.stdout.splitlines() if ": error:" in line]
    assert len(errors) == 2, result.stdout
    assert any(":6: error:" in line and "[type-var]" in line for line in errors), result.stdout
    assert any(":7: error:" in line and "[arg-type]" in line for line in errors), result.stdout


def test_public_status_types(tmp_path):
    """
    Expose nested metrics as concrete types to library and installed-wheel consumers.
    """
    consumer = tmp_path / "status_consumer.py"
    consumer.write_text(
        "from typing import assert_type\n"
        "from polyad_types.resources import GraphMetrics, TopologyMetrics, ExecutionMetrics\n"
        "from polyad.compiler.passes.schema import structural_schema\n"
        "metrics = GraphMetrics(observedGeneration=2)\n"
        "assert_type(metrics.topology, TopologyMetrics | None)\n"
        "assert_type(metrics.execution, ExecutionMetrics | None)\n"
        "assert_type(metrics.resources.byKind.Job, int)\n"
        "assert_type(metrics.rollup.graphsByPhase.Ready, int)\n"
        "schema = structural_schema(GraphMetrics)\n"
    )
    result = check_types(consumer)
    assert result.returncode == 0, result.stdout + result.stderr


def test_installed_wheel_has_inline_types(tmp_path):
    """
    Verify CI's consumer imports a marked installed package, outside the source checkout.
    """
    interpreter = os.environ.get("POLYAD_TYPING_PYTHON")
    if interpreter is None:
        # Source runs still enforce the package marker; CI additionally checks its wheel.
        assert (ROOT / "pkg/polyad/py.typed").is_file()
        return
    result = subprocess.run(
        [interpreter, "-c", "import polyad; print(polyad.__file__)"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    package = Path(result.stdout.strip()).parent
    assert "site-packages" in package.parts, package
    assert not package.is_relative_to(ROOT / "pkg"), package
    assert (package / "py.typed").is_file()
