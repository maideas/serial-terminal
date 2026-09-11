"""Unit tests for serial_common (no hardware needed)."""

import argparse
import errno
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import serial_common as sc
import serial_tui


def tx_args(eol="crlf", encoding="utf-8"):
    return SimpleNamespace(eol=eol, encoding=encoding)


# --- build_tx_payload ----------------------------------------------------
def test_text_gets_eol():
    assert sc.build_tx_payload("GET TEMP", tx_args()) == b"GET TEMP\r\n"
    assert sc.build_tx_payload("x", tx_args("none")) == b"x"


def test_hex_command():
    assert sc.build_tx_payload(r"\hex 41 42 0d", tx_args("lf")) == b"AB\r\n"
    assert sc.build_tx_payload(r"\HEX 41", tx_args("none")) == b"A"


@pytest.mark.parametrize("bad", [r"\hex", r"\hex 4 1", r"\hex 4142", r"\hex zz", r"\hex 41 4"])
def test_hex_rejects_malformed(bad):
    with pytest.raises(sc.InvalidHexError):
        sc.build_tx_payload(bad, tx_args())


def test_hexdump_prefix_is_plain_text():
    # F7: "\hexdump" must not be mistaken for the \hex command
    assert sc.build_tx_payload(r"\hexdump 41", tx_args("none")) == rb"\hexdump 41"


def test_quit():
    assert sc.build_tx_payload(r"\quit", tx_args()) is None
    assert sc.build_tx_payload(r"\QUIT", tx_args()) is None
    assert sc.build_tx_payload(r"\quit now", tx_args("none")) == rb"\quit now"


def test_commands_accept_tab_separator():
    # tab counts as the command separator, not part of the command word
    assert sc.build_tx_payload("\\hex\t41 42", tx_args("none")) == b"AB"
    assert sc.build_tx_payload("\\quit\t", tx_args()) is None


def test_literal_backslash_escape():
    assert sc.build_tx_payload(r"\\hex 41", tx_args("none")) == rb"\hex 41"


def test_encode_error_propagates():
    with pytest.raises(UnicodeEncodeError):
        sc.build_tx_payload("ü", tx_args(encoding="ascii"))


# --- sanitize / decode ---------------------------------------------------
def test_sanitize_escapes_control_chars():
    assert sc.sanitize("\x1b]0;evil\x07ok\x1b[2J") == r"\x1b]0;evil\x07ok\x1b[2J"
    assert sc.sanitize("a\tb") == "a\tb"
    assert sc.sanitize("plain") == "plain"


def test_decode_rx_line_strips_eol_and_replaces_bad_bytes():
    args = SimpleNamespace(encoding="utf-8")
    assert sc.decode_rx_line(b"ok\r\n", args) == "ok"
    assert sc.decode_rx_line(b"\xff\n", args) == "\ufffd"


# --- open error classification -----------------------------------------
class FakeExc(Exception):
    def __init__(self, msg, errno_=None):
        super().__init__(msg)
        self.errno = errno_


def test_classify_posix_errnos():
    assert sc.classify_open_error(FakeExc("x", errno.ENOENT)) == "missing"
    assert sc.classify_open_error(FakeExc("x", errno.EACCES)) == "busy"
    assert sc.classify_open_error(FakeExc("x", errno.EBUSY)) == "busy"
    assert sc.classify_open_error(FakeExc("x", errno.EIO)) is None


def test_classify_windows_messages():
    denied = "could not open port 'COM13': PermissionError(13, 'Access is denied.', None, 5)"
    # F2: no false positive from "13" in the port name / PermissionError code
    assert sc.classify_open_error(FakeExc(denied)) is None
    assert sc.classify_open_error(FakeExc("[WinError 5] Access is denied")) == "busy"
    assert sc.classify_open_error(FakeExc("[WinError 2] The system cannot find")) == "missing"
    assert sc.classify_open_error(FakeExc("[WinError 32] sharing violation")) == "busy"


# --- argparse validators -------------------------------------------------
def test_validators():
    assert sc.positive_int("9600") == 9600
    assert sc.positive_float("0.5") == 0.5
    assert sc.encoding_name("latin-1") == "latin-1"
    for fn, bad in [
        (sc.positive_int, "0"),
        (sc.positive_int, "-1"),
        (sc.positive_int, "x"),
        (sc.positive_float, "0"),
        (sc.positive_float, "-1"),
        (sc.encoding_name, "bogus"),
    ]:
        with pytest.raises(argparse.ArgumentTypeError):
            fn(bad)


def test_common_parser_rejects_bad_values():
    p = argparse.ArgumentParser()
    sc.add_common_args(p, tx=True)
    with pytest.raises(SystemExit):
        p.parse_args(["--timeout", "0"])
    with pytest.raises(SystemExit):
        p.parse_args(["--encoding", "bogus"])
    ns = p.parse_args(["-p", "COM3", "--eol", "lf"])
    assert ns.eol == "lf" and ns.timeout == 1.0


# --- TUI view helper -----------------------------------------------------
def test_visible_rx_lines_wraps_and_clamps():
    entries = [{"text": "a" * 10, "hex": b"AB"}, {"text": "short", "hex": None}]
    # display lines: aaaa aaaa aa shor t  -> 5 total, 2 fit
    lines, max_off = serial_tui.visible_rx_lines(entries, 4, False, 0, 2)
    assert lines == ["shor", "t"]
    assert max_off == 3
    # offset beyond the top is clamped to max_offset
    lines, _ = serial_tui.visible_rx_lines(entries, 4, False, 999, 2)
    assert lines == ["aaaa", "aaaa"]


def test_visible_rx_lines_hexdump_toggle_rewraps():
    entries = [{"text": "t", "hex": b"\x01"}]
    lines, _ = serial_tui.visible_rx_lines(entries, 80, True, 0, 5)
    assert lines == ["t", "hex: 01"]
    lines, _ = serial_tui.visible_rx_lines(entries, 80, False, 0, 5)
    assert lines == ["t"]


def test_terminal_encodable_replaces_unencodable():
    # a CJK / degree character on an ASCII-locale console must not crash addnstr
    assert serial_tui._terminal_encodable("21.7 \u00b0C \u65e5", "ascii") == "21.7 ?C ?"
    assert serial_tui._terminal_encodable("21.7 \u00b0C", "utf-8") == "21.7 \u00b0C"
