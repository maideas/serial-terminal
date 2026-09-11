#!/usr/bin/env python3
"""Smoke test for serial_tui.py: drive it in a pty-backed terminal against a pty "device".

The TUI is a curses program, so it needs a real terminal: pty.fork() gives it
one. Two rules keep this harness from deadlocking:
  * the terminal master is drained continuously by a thread (a curses child
    blocks once the pty buffer is full, and would then never read our keys),
  * every read from the device side uses select() with a timeout.
"""

import os
import pty
import re
import select
import sys
import threading
import time

ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][0-9A-Za-z]|\x1b[=>]|\x1b\][^\x07]*\x07")
DEADLINE = 30  # hard cap for the whole run


def main():
    dev_master, dev_slave = pty.openpty()
    dev_name = os.ttyname(dev_slave)

    pid, term_fd = pty.fork()
    if pid == 0:  # child: the TUI, with the pty as its controlling terminal
        os.environ.update(TERM="xterm", LINES="24", COLUMNS="80")
        os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        os.execv(
            sys.executable,
            [sys.executable, "serial_tui.py", "-p", dev_name, "--eol", "lf", "--timestamps"],
        )

    screen = bytearray()
    lock = threading.Lock()
    done = threading.Event()

    def drain():
        while not done.is_set():
            if select.select([term_fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(term_fd, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                with lock:
                    screen.extend(chunk)

    drainer = threading.Thread(target=drain, daemon=True)
    drainer.start()

    def visible():
        with lock:
            return ANSI.sub(b"", bytes(screen)).decode(errors="replace")

    def raw():
        with lock:
            return bytes(screen)

    def read_device(timeout=1.0):
        out = b""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if select.select([dev_master], [], [], 0.1)[0]:
                try:
                    out += os.read(dev_master, 4096)
                except OSError:
                    break
        return out

    failures = []

    def check(label, ok):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            failures.append(label)

    try:
        time.sleep(1.2)  # let curses paint the first frame

        # 1. RX display + escape-sequence sanitising
        os.write(dev_master, b"TEMP=21.7C\n\x1b[2Jevil\x1b]0;PWNED\x07\n")
        time.sleep(0.6)
        print("RX pane:")
        check("received line shown", "TEMP=21.7C" in visible())
        check("escape sequence shown escaped", r"\x1b[2Jevil" in visible())
        # the device's OSC title sequence must never reach the terminal as
        # raw bytes; as escaped text ("\\x1b]0;PWNED\\x07") it is inert
        check("no raw OSC sequence from device on the wire", b"\x1b]0;PWNED" not in raw())
        check("OSC sequence rendered as inert text", r"\x1b]0;PWNED\x07" in visible())
        check("RX pane and TX prompt drawn", "RX" in visible() and "TX>" in visible())

        # 2. TX: plain text, valid hex, invalid hex
        os.write(term_fd, b"GET TEMP\r")
        check("plain text sent with LF", read_device() == b"GET TEMP\n")
        os.write(term_fd, b"\\hex 41 42\r")
        check("hex command sent raw bytes", read_device() == b"AB\n")
        os.write(term_fd, b"\\hex 4 1\r")
        time.sleep(0.6)
        check("malformed hex reported in RX pane", "invalid hex" in visible())
        check("malformed hex sent nothing", read_device(0.4) == b"")

        # 3. scrolling does not wander off the top
        for _ in range(20):
            os.write(term_fd, b"\x1b[5~")  # PgUp
        time.sleep(0.4)
        check("still alive after excessive PgUp", "RX" in visible())
        os.write(term_fd, b"\x1bOF")  # End (xterm terminfo kend)
        time.sleep(0.3)

        # 4. unplug: the TUI must exit and report the reason
        os.close(dev_master)
        os.close(dev_slave)
        exited = None
        end = time.monotonic() + 8
        while time.monotonic() < end:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid:
                exited = os.waitstatus_to_exitcode(status)
                break
            time.sleep(0.2)
        print("unplug handling:")
        check("TUI exited on its own", exited is not None)
        time.sleep(0.3)
        tail = visible()
        check("exit code 1", exited == 1)
        check("reason reported", "Serial read failed" in tail or "disconnected" in tail)
        check("no traceback", "Traceback" not in tail)
        if failures:
            print("\nlast screen text:\n" + "\n".join(tail.splitlines()[-8:]))
    finally:
        done.set()
        try:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'all smoke checks passed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
