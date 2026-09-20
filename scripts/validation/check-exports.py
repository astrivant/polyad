"""
Require explicit, dependency-free public export inventories in packaged modules.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_DIRECTORIES = (
    "pkg/polyad",
    "pkg/polyad-sdk/polyad_sdk",
    "pkg/polyad-types/polyad_types",
    "pkg/polyad-schemas/polyad_schemas",
    "pkg/polyad-benchmarks/polyad_benchmarks",
)
PACKAGE_NAMES = frozenset(Path(directory).name for directory in PACKAGE_DIRECTORIES)


def package_files(project: Path = ROOT) -> tuple[Path, ...]:
    """
    Find shipped Python modules without importing optional dependencies or applications.

    Args:
        project (Path): Repository containing the independently installable packages.

    Returns:
        tuple[Path, ...]: Ordered implementation and initializer paths, excluding tests and caches.
    """
    return tuple(sorted(path for directory in PACKAGE_DIRECTORIES for path in (project / directory).rglob("*.py")))


def module_statements(nodes: list[ast.stmt]) -> Iterator[ast.stmt]:
    """
    Traverse module-level control flow without entering class or function scopes.

    Args:
        nodes (list[ast.stmt]): Statements in a module or its conditional blocks.

    Yields:
        ast.stmt: Module-scope statements, including guarded imports used by lazy exports.
    """
    for node in nodes:
        yield node
        if isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.TryStar)):
            for field in ("body", "orelse", "finalbody"):
                yield from module_statements(getattr(node, field, []))
            if isinstance(node, (ast.Try, ast.TryStar)):
                for handler in node.handlers:
                    yield from module_statements(handler.body)
        elif isinstance(node, ast.Match):
            for case in node.cases:
                yield from module_statements(case.body)


def bindings(tree: ast.Module) -> dict[str, str | None]:
    """
    Distinguish locally declared symbols from imported implementation dependencies.

    Args:
        tree (ast.Module): Parsed source to inspect without executing it.

    Returns:
        dict[str, str | None]: Binding names mapped to import origins, or None for local declarations.
    """
    result: dict[str, str | None] = {}
    for node in module_statements(tree.body):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            result[node.name] = None
        elif isinstance(node, ast.Import):
            for alias in node.names:
                result[alias.asname or alias.name.split(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                result[alias.asname or alias.name] = "." * node.level + (node.module or "")
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.TypeAlias)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.name if isinstance(node, ast.TypeAlias) else node.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                        result[name.id] = None
    return result


def violations(path: Path) -> list[str]:
    """
    Validate a literal export inventory and reject dependency re-exports.

    Explicit inventories may include locally defined types, constants, functions,
    configured objects and deliberate re-exports from another Polyad package.
    Lazy export resolution is exercised by runtime tests instead of importing
    dependency-heavy modules during linting.

    Args:
        path (Path): Python package module to inspect.

    Returns:
        list[str]: Actionable errors; an empty list means the inventory is well formed.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    declarations = [
        node
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == "__all__"
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]
    if len(declarations) != 1:
        return ["declare exactly one module-level __all__ inventory"]
    value = declarations[0].value
    if not isinstance(value, (ast.Tuple, ast.List)) or any(
        not isinstance(item, ast.Constant) or not isinstance(item.value, str) for item in value.elts
    ):
        return ["__all__ must be a literal tuple or list of string names"]
    exports = ast.literal_eval(value)
    errors = []
    if len(exports) != len(set(exports)):
        errors.append("__all__ contains duplicate names")
    origins = bindings(tree)
    for name in exports:
        if name.startswith("_") or not name.isidentifier():
            errors.append(f"{name!r} is not a public identifier")
        elif name not in origins:
            errors.append(f"{name!r} has no declared module binding")
        elif (origin := origins[name]) is not None and not origin.startswith(".") and origin.split(".")[0] not in PACKAGE_NAMES:
            errors.append(f"{name!r} re-exports an import from {origin!r}; consumers should import that dependency directly")

    # Keep the inventory static: imports and module loading order must not change
    # what an application receives from a wildcard import.
    for node in module_statements(tree.body):
        if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) and node.target.id == "__all__":
            errors.append("do not extend __all__ dynamically")
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            function = node.value.func
            if isinstance(function, ast.Attribute) and isinstance(function.value, ast.Name) and function.value.id == "__all__":
                errors.append("do not mutate __all__ dynamically")
    return errors


def main() -> int:
    """
    Check all five distribution inventories without importing their runtime dependencies.

    Returns:
        int: Zero for valid export declarations, or one after reporting every violation.
    """
    files = package_files()
    errors = [(path, error) for path in files for error in violations(path)]
    for path, error in errors:
        print(f"{path.relative_to(ROOT)}: {error}")
    if not errors:
        print(f"Validated explicit public exports in {len(files)} package modules")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
