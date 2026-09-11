"""Runs tests/smoke_tui.py, which drives serial_tui.py in a pty-backed terminal."""

import subprocess
import sys
from pathlib import Path

import pytest

SMOKE = Path(__file__).resolve().parent / "smoke_tui.py"

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX only")


def test_tui_smoke():
    proc = subprocess.run(
        [sys.executable, str(SMOKE)], capture_output=True, text=True, timeout=90, check=False
    )
    if proc.returncode != 0:
        pytest.fail(f"TUI smoke test failed:\n{proc.stdout}\n{proc.stderr}")
