"""Integration tests: run the plain-terminal tools against a pty posing as the device."""

import os
import pty
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONUNBUFFERED": "1"}

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX only")


def start(tool, *extra):
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / tool), "-p", os.ttyname(slave), "--timeout", "0.2", *extra],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=ENV,
    )
    time.sleep(0.6)
    return proc, master, slave


def finish(proc, timeout=4):
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        pytest.fail("tool did not exit: " + out.decode(errors="replace"))
    return out.decode(errors="replace")


def test_reader_sanitizes_escape_sequences():
    proc, master, _slave = start("serial_reader.py")
    os.write(master, b"\x1b]0;PWNED\x07text\x1b[2J\n")
    time.sleep(0.4)
    proc.terminate()
    out = finish(proc)
    assert "\x1b" not in out
    assert r"\x1b]0;PWNED\x07text\x1b[2J" in out


def test_reader_reports_unplug():
    proc, master, slave = start("serial_reader.py")
    os.write(master, b"first\n")
    time.sleep(0.3)
    os.close(master)
    os.close(slave)
    out = finish(proc)
    assert "first" in out
    assert "Serial error" in out
    assert "Traceback" not in out
    assert proc.returncode == 1


def test_terminal_roundtrip_and_hex():
    proc, master, _slave = start("serial_terminal.py", "--eol", "lf")
    proc.stdin.write(b"hello\n\\hex 41 42\n\\hex zz\n\\quit\n")
    proc.stdin.flush()
    time.sleep(0.5)
    sent = os.read(master, 100)
    out = finish(proc)
    assert sent == b"hello\nAB\n"
    assert "invalid hex" in out
    assert "Disconnected. Bye." in out
    assert proc.returncode == 0


def test_terminal_unplug_then_enter_no_traceback():
    proc, master, slave = start("serial_terminal.py")
    os.close(master)
    os.close(slave)
    time.sleep(0.8)
    try:
        proc.stdin.write(b"typed after unplug\n")
        proc.stdin.flush()
    except BrokenPipeError:
        pass
    out = finish(proc)
    assert "Traceback" not in out
    assert "device disconnected" in out
    assert proc.returncode == 1


def test_bad_open_errors_are_classified(tmp_path):
    missing = subprocess.run(
        [sys.executable, str(ROOT / "serial_reader.py"), "-p", str(tmp_path / "nope")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "does not exist" in missing.stderr and missing.returncode == 1

    noperm = tmp_path / "noperm"
    noperm.touch()
    noperm.chmod(0)
    if os.access(noperm, os.R_OK):  # running as root: EACCES cannot be provoked
        pytest.skip("cannot provoke EACCES as root")
    denied = subprocess.run(
        [sys.executable, str(ROOT / "serial_reader.py"), "-p", str(noperm)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert "access denied" in denied.stderr and denied.returncode == 1
