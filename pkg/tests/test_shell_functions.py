"""
Check shell function documentation without confusing heredocs with executable code.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(shutil.which("shfmt") is None, reason="requires the shell parser")


@pytest.mark.parametrize(
    ("source", "valid"),
    [
        ("##\n# Print usage.\n# -> ret::exit_code\nusage() {\n    :\n}\n", True),
        ("##\n# Execute a command.\n# command::string args::string[] -> ret::exit_code\nrun() {\n    :\n}\n", True),
        ("cat <<'EOF'\nnot_a_function() {\nEOF\n", True),
        ("undocumented() {\n    :\n}\n", False),
        ("##\n# Missing typed return.\n# name::string\nbad() {\n    :\n}\n", False),
        ("##\n# Bad spacing.\n#-> ret::exit_code\nbad() {\n    :\n}\n", False),
        ("##\n# Wrong opening.\n# -> ret::exit_code\nfunction bad() {\n    :\n}\n", False),
        ("##\n# Multiline body required.\n# -> ret::exit_code\nbad() { :; }\n", False),
        ("##\n# No separator after the signature.\n# -> ret::exit_code\n\nbad() {\n    :\n}\n", False),
        ("##\n# Outer function.\n# -> ret::exit_code\nouter() {\n    nested() {\n        :\n    }\n}\n", False),
    ],
)
def test_shell_headers(tmp_path: Path, source: str, valid: bool) -> None:
    """
    Accept documented functions and reject incomplete headers using the real shell parser.

    Args:
        tmp_path (Path): Isolated source file directory.
        source (str): Shell source to validate, never execute.
        valid (bool): Whether the supplied source conforms to the header convention.
    """
    path = tmp_path / "source.sh"
    path.write_text(source)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validation/check-shell-functions.py"), str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is valid, result.stdout + result.stderr
