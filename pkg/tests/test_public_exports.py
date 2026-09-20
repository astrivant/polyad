"""
Keep packaged APIs explicit without leaking dependencies or eager optional imports.
"""

from __future__ import annotations

import ast
import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHECKER = runpy.run_path(str(ROOT / "scripts/validation/check-exports.py"))


@pytest.mark.parametrize("path", CHECKER["package_files"](), ids=lambda path: str(path.relative_to(ROOT)))
def test_every_package_module_has_a_dependency_free_export_inventory(path):
    """
    Check implementation modules and package roots without importing optional backends.
    """
    assert CHECKER["violations"](path) == []


@pytest.mark.parametrize(
    "source,message",
    [
        ("def public(): pass\n", "exactly one"),
        ("__all__ = tuple()\n", "literal"),
        ("__all__ = ('missing',)\n", "no declared"),
        ("def public(): pass\n__all__ = ('public', 'public')\n", "duplicate"),
        ("_private = 1\n__all__ = ('_private',)\n", "public identifier"),
        ("import numpy as np\n__all__ = ('np',)\n", "import from 'numpy'"),
        ("from attrs import frozen as model\n__all__ = ('model',)\n", "import from 'attrs'"),
        ("from typing import Any\n__all__ = ('Any',)\n", "import from 'typing'"),
        (
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from pathlib import Path\n__all__ = ('Path',)\n",
            "import from 'pathlib'",
        ),
        ("VALUE = 1\n__all__ = ('VALUE',)\n__all__ += ('VALUE',)\n", "extend"),
        ("VALUE = 1\n__all__ = ['VALUE']\n__all__.append('VALUE')\n", "mutate"),
    ],
)
def test_export_linter_rejects_ambiguous_or_foreign_apis(tmp_path, source, message):
    """
    Reject both third-party and standard-library implementation imports from exports.
    """
    path = tmp_path / "module.py"
    path.write_text(source)
    assert any(message in error for error in CHECKER["violations"](path))


@pytest.mark.parametrize(
    "source",
    [
        "__all__ = ()\n",
        "VALUE = 1\ndef public(): pass\n__all__ = ('VALUE', 'public')\n",
        "from polyad_types import ServiceEndpoint\n__all__ = ('ServiceEndpoint',)\n",
        "from .models import Model\n__all__ = ('Model',)\n",
        "from cattrs import Converter\nconverter = Converter()\n__all__ = ('converter',)\n",
        "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from polyad.cache.redis import Cache\n"
        "def __getattr__(name): pass\n__all__ = ('Cache',)\n",
    ],
)
def test_export_linter_preserves_owned_objects_and_intentional_reexports(tmp_path, source):
    """
    Public configured objects are distinct from re-exporting their dependency classes.
    """
    path = tmp_path / "module.py"
    path.write_text(source)
    assert CHECKER["violations"](path) == []


def test_runtime_wildcard_exports_exist_and_resolve_only_declared_names():
    """
    Exercise actual wildcard imports and lazy exports in an isolated interpreter.
    """
    modules = []
    for directory in CHECKER["PACKAGE_DIRECTORIES"]:
        root = ROOT / directory
        for path in root.rglob("*.py"):
            parts = list(path.relative_to(root.parent).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            modules.append(".".join(parts))
    script = """
import importlib
import inspect
import sys

packages = {'polyad', 'polyad_sdk', 'polyad_types', 'polyad_schemas', 'polyad_benchmarks'}
for name in sys.argv[1:]:
    module = importlib.import_module(name)
    namespace = {}
    exec(f'from {name} import *', namespace)
    assert set(namespace) - {'__builtins__'} == set(module.__all__), name
    for export in module.__all__:
        value = namespace[export]
        assert value is getattr(module, export), (name, export)
        if inspect.isclass(value) or inspect.isfunction(value):
            assert value.__module__.split('.')[0] in packages, (name, export, value.__module__)
        elif inspect.ismodule(value):
            assert value.__name__.split('.')[0] in packages, (name, export)
"""
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT), *(str((ROOT / directory).parent) for directory in CHECKER["PACKAGE_DIRECTORIES"])]
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, *sorted(modules)], cwd=ROOT, env=environment, text=True, capture_output=True, timeout=90
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_sdk_wildcard_imports_keep_optional_dependencies_lazy():
    """
    Export discovery must not initialize numerical engines, protocol drivers or operator libraries.
    """
    script = """
import importlib
import pkgutil
import sys
import polyad_sdk
for module in pkgutil.walk_packages(polyad_sdk.__path__, 'polyad_sdk.'):
    namespace = {}
    exec(f'from {module.name} import *', namespace)
assert not {'grpc', 'pika', 'redis', 'numpy', 'jax', 'hj_reachability', 'kubernetes', 'kopf', 'polyad'}.intersection(sys.modules)
from polyad_sdk import AdaptiveService, WorkloadClient, container_metrics
from polyad_sdk.symbiosis.service import AdaptiveService as Implementation
assert AdaptiveService is Implementation
"""
    subprocess.run([sys.executable, "-c", script], cwd=ROOT, check=True, timeout=30)


def test_existing_self_aliased_polyad_reexports_remain_public():
    """
    Preserve the deliberately declared first-party compatibility imports throughout the packages.
    """
    for path in CHECKER["package_files"]():
        tree = ast.parse(path.read_text())
        export_node = next(
            node
            for node in tree.body
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
        )
        exported = set(ast.literal_eval(export_node.value))
        for node in CHECKER["module_statements"](tree.body):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in CHECKER["PACKAGE_NAMES"]:
                for alias in node.names:
                    if alias.asname == alias.name and not alias.name.startswith("_"):
                        assert alias.name in exported, (path, alias.name)
