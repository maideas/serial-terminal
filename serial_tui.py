#!/usr/bin/env python3
"""Split-screen TUI terminal for USB-to-UART adapters (RX + TX panes).

Two separate areas, so received and transmitted content never mix on screen:

    COM3 @ 115200 8N1 | eol=CRLF | F1:hex on | PgUp/PgDn: scroll
    +--- RX ---------------------------------------------------------
    | 20:12:36 heartbeat 2
    | 20:12:37 TEMP=21.7C
    +----------------------------------------------------------------
    TX> GET TEMP_
     Enter: send | \\hex 41 42: raw bytes | Ctrl+C: quit

Usage examples (Windows):
    py serial_tui.py                       # interactive port picker, 115200 8N1
    py serial_tui.py --list
    py serial_tui.py -p COM3 -b 9600
    py serial_tui.py -p COM3 --eol lf --hexdump

Usage examples (Linux/macOS):
    python3 serial_tui.py -p /dev/ttyUSB0 -b 9600

Requirements:  pyserial and serial_common.py next to this file;
               Windows additionally:  py -m pip install windows-curses
               (Linux/macOS: curses is built in).
               Or install the package:  pip install .  →  serial-tui
               (windows-curses is then pulled in automatically on Windows).

Keys:
    printable chars / Backspace   edit the TX line
    Enter                         send the TX line (with configured EOL)
    \\hex 41 42                    send raw bytes (+ EOL)
    \\quit                         quit
    \\\\text                        send a line starting with a literal backslash
    F1                            toggle hex dump of received lines
    PgUp / PgDn                   scroll the RX pane (End jumps back to tail)
    Ctrl+C                        quit
"""

import argparse
import collections
import contextlib
import queue
import sys
import threading
import time

from serial_common import (
    RX_HISTORY_MAX,
    InvalidHexError,
    add_common_args,
    build_tx_payload,
    choose_port,
    decode_rx_line,
    describe_port,
    hexdump_line,
    list_available_ports,
    open_port,
    serial,
)

try:
    import curses
except ImportError:
    sys.exit(
        "The curses module is missing - on Windows run:  "
        "py -m pip install windows-curses  "
        "(on Linux/macOS curses is part of the Python standard "
        "library and should always be available)"
    )

# The TUI polls the keyboard every 100 ms, so a shorter serial timeout than
# the plain tools keeps shutdown snappy without costing CPU.
DEFAULT_TIMEOUT = 0.5
SCROLL_STEP = 10  # display lines per PgUp/PgDn


def parse_args():
    p = argparse.ArgumentParser(
        description="Split-screen RX/TX terminal for a USB serial adapter.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_common_args(p, tx=True, default_timeout=DEFAULT_TIMEOUT)
    return p.parse_args()


def reader_loop(ser, args, rx_q, stop_event, error):
    """Background thread: put every received line into the RX queue.

    On failure the reason is stored in `error["reader"]` and stop_event is
    set; main() prints it after curses has restored the terminal.
    """
    while not stop_event.is_set():
        try:
            line = ser.readline()  # returns b"" on timeout
            if not line:
                continue
            prefix = time.strftime("%H:%M:%S ") if args.timestamps else ""
            rx_q.put({"text": prefix + decode_rx_line(line, args), "hex": line})
        except Exception as exc:  # a dead RX thread must always be visible
            # SerialException/OSError: adapter unplugged; TypeError: fd
            # closed under us while exiting (POSIX). Anything else is a bug,
            # but must still be reported rather than dying silently.
            if not stop_event.is_set():
                error["reader"] = f"{type(exc).__name__}: {exc}"
                stop_event.set()
            return


def _wrap(text, width):
    lines = []
    while text:
        lines.append(text[:width])
        text = text[width:]
    return lines


def _terminal_encodable(text, encoding):
    """Replace characters the terminal cannot display.

    curses encodes with the *locale* encoding, which can be narrower than
    the serial --encoding (e.g. an ASCII/C locale console receiving CJK);
    a plain addnstr would raise UnicodeEncodeError and kill the TUI.
    """
    try:
        text.encode(encoding)
    except UnicodeEncodeError:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    return text


def visible_rx_lines(entries, width, hexdump, offset, inner_h):
    """Flatten entries into display lines (wrapped), return the visible slice.

    `offset` = number of display lines scrolled up from the tail (0 = follow).
    Returns (lines, max_offset): `lines` oldest-first so the newest line sits
    at the bottom; `max_offset` is the largest offset that still shows
    content, so the caller can clamp scrolling at the top of the history.
    """
    flat = []
    for e in entries:
        cache = e.get("wrap_cache")
        if cache is None or cache[0] != (width, hexdump):
            lines = _wrap(e["text"], width)
            if hexdump and e.get("hex") is not None:
                lines.extend(_wrap(hexdump_line(e["hex"]), width))
            cache = ((width, hexdump), lines)
            e["wrap_cache"] = cache
        flat.extend(cache[1])
    max_offset = max(0, len(flat) - inner_h)
    offset = min(offset, max_offset)
    start = max(0, len(flat) - offset - inner_h)
    return flat[start : start + inner_h], max_offset


def tui_main(stdscr, ser, args, rx_q, stop_event, error):  # noqa: PLR0913, PLR0917
    curses.curs_set(1)
    stdscr.timeout(100)  # poll getch every 100 ms
    stdscr.keypad(True)

    input_buf = ""
    offset = 0  # scroll offset in display lines from the tail
    max_offset = 0
    hexdump = args.hexdump
    # received entries: {"text": str, "hex": bytes|None}; bounded so a long
    # session cannot grow without limit
    entries = collections.deque(maxlen=RX_HISTORY_MAX)

    while not stop_event.is_set():
        # --- input events --------------------------------------------
        quit_flag = False
        while True:
            ch = stdscr.getch()
            if ch == -1:
                break
            if ch in (3, 4):
                # Ctrl+D; Ctrl+C normally arrives as KeyboardInterrupt (see
                # main), but some curses builds deliver it as byte 3
                quit_flag = True
            elif ch in (10, 13, curses.KEY_ENTER):
                if input_buf:
                    try:
                        payload = build_tx_payload(input_buf, args)
                    except InvalidHexError as exc:
                        entries.append({"text": f"[invalid hex, nothing sent: {exc}]", "hex": None})
                        payload = b""
                    except UnicodeEncodeError as exc:
                        entries.append(
                            {"text": f"[cannot encode as {args.encoding}: {exc}]", "hex": None}
                        )
                        payload = b""
                    if payload is None:  # \quit
                        quit_flag = True
                    elif payload:
                        try:
                            ser.write(payload)
                            ser.flush()
                        except (serial.SerialException, OSError) as exc:
                            error["writer"] = str(exc)
                            stop_event.set()
                            quit_flag = True
                    input_buf = ""
                    offset = 0
            elif ch in (8, 127, curses.KEY_BACKSPACE):
                input_buf = input_buf[:-1]
            elif ch == curses.KEY_PPAGE:
                offset = min(offset + SCROLL_STEP, max_offset)
            elif ch == curses.KEY_NPAGE:
                offset = max(0, offset - SCROLL_STEP)
            elif ch == curses.KEY_END:
                offset = 0
            elif ch == curses.KEY_F0 + 1:
                hexdump = not hexdump  # wrap cache is keyed on hexdump, re-wraps itself
            elif 32 <= ch <= 126:
                input_buf += chr(ch)
        if quit_flag:
            break

        # --- drain RX queue -------------------------------------------
        try:
            while True:
                entries.append(rx_q.get_nowait())
        except queue.Empty:
            pass

        # --- draw -------------------------------------------------------
        h, w = stdscr.getmaxyx()
        if h < 8 or w < 30:
            stdscr.erase()
            stdscr.addstr(0, 0, "Terminal too small (need >= 30x8).")
            stdscr.refresh()
            continue

        status = (
            f" {describe_port(args)}"
            f" | eol={args.eol.upper()}"
            f" | F1:hex {'on' if hexdump else 'off'}"
            f" | PgUp/PgDn: scroll"
        )
        stdscr.erase()
        stdscr.addnstr(0, 0, status.ljust(w - 1), w - 1, curses.A_REVERSE)

        # RX pane with border (rows 1 .. h-4)
        rx_h = h - 4
        rx_win = stdscr.derwin(rx_h, w, 1, 0)
        rx_win.border()
        rx_win.addnstr(0, 2, " RX ", 4, curses.A_BOLD)
        inner_h = rx_h - 2
        inner_w = w - 4
        vis, max_offset = visible_rx_lines(entries, inner_w, hexdump, offset, inner_h)
        offset = min(offset, max_offset)
        enc = getattr(rx_win, "encoding", None) or getattr(sys.stdout, "encoding", None) or "utf-8"
        for i, txt in enumerate(vis):
            rx_win.addnstr(1 + i, 1, _terminal_encodable(txt, enc), inner_w, curses.A_NORMAL)
        rx_win.refresh()

        # TX input line (row h-3) and hints (row h-2)
        stdscr.addnstr(h - 3, 0, "TX> ", 4, curses.A_BOLD)
        visible_input = input_buf[-(w - 5) :]
        stdscr.addnstr(h - 3, 4, visible_input, w - 5, curses.A_NORMAL)
        hints = " Enter: send | \\hex 41 42: raw bytes | End: tail | Ctrl+C: quit"
        stdscr.addnstr(h - 2, 0, hints, w - 1, curses.A_DIM)

        # put the hardware cursor at the end of the TX input
        stdscr.move(h - 3, 4 + len(visible_input))
        stdscr.refresh()


def main():
    args = parse_args()

    if args.list:
        list_available_ports()
        return

    args.port = choose_port(args.port)
    ser = open_port(args)
    ser.reset_input_buffer()

    rx_q = queue.Queue()
    stop_event = threading.Event()
    error = {"reader": None, "writer": None}
    reader = threading.Thread(
        target=reader_loop, args=(ser, args, rx_q, stop_event, error), daemon=True
    )
    reader.start()

    try:
        curses.wrapper(tui_main, ser, args, rx_q, stop_event, error)
    except KeyboardInterrupt:
        pass  # Ctrl+C: curses.wrapper has already restored the terminal
    finally:
        stop_event.set()
        with contextlib.suppress(Exception):  # best effort, port may already be gone
            ser.close()
        reader.join(timeout=2)
    if error["writer"]:
        sys.exit(f"Serial write failed (device disconnected?): {error['writer']}")
    if error["reader"]:
        sys.exit(f"Serial read failed (device disconnected?): {error['reader']}")
    print("Disconnected. Bye.")


if __name__ == "__main__":
    main()
