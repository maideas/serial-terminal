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

Usage examples:
    py serial_tui.py                       # interactive port picker, 115200 8N1
    py serial_tui.py --list
    py serial_tui.py -p COM3 -b 9600
    py serial_tui.py -p COM3 --eol lf --hexdump

Requirements:  py -m pip install pyserial windows-curses
               (on Linux/macOS curses is built in: just pyserial)

Keys:
    printable chars / Backspace   edit the TX line
    Enter                         send the TX line (with configured EOL)
    \\hex 41 42                    send raw bytes (+ EOL)
    F1                            toggle hex dump of received lines
    PgUp / PgDn                   scroll the RX pane (End jumps back to tail)
    Ctrl+C                        quit
"""

import argparse
import queue
import sys
import threading
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is not installed. Run:  pip install pyserial  "
             "(or:  py -m pip install pyserial)")

try:
    import curses
except ImportError:
    sys.exit("The curses module is missing (Windows). Run:  "
             "py -m pip install windows-curses")

EOL_CHOICES = {
    "crlf": b"\r\n",   # default for most devices / classic terminals
    "lf":   b"\n",
    "cr":   b"\r",
    "none": b"",
}


def parse_args():
    p = argparse.ArgumentParser(
        description="Split-screen RX/TX terminal for a USB serial adapter.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-p", "--port", metavar="COMx",
                   help="serial port (default: interactive picker, or the "
                        "only port if there is exactly one)")
    p.add_argument("-b", "--baud", type=int, default=115200,
                   help="baud rate")
    p.add_argument("--bytesize", type=int, choices=[5, 6, 7, 8], default=8,
                   help="number of data bits")
    p.add_argument("--parity", choices=list("NEOMS"), default="N",
                   help="parity: N=none, E=even, O=odd, M=mark, S=space")
    p.add_argument("--stopbits", type=float, choices=[1, 1.5, 2], default=1,
                   help="number of stop bits")
    p.add_argument("--timeout", type=float, default=0.5,
                   help="serial read timeout in seconds")
    p.add_argument("--eol", choices=list(EOL_CHOICES), default="crlf",
                   help="line ending appended to what you send")
    p.add_argument("--encoding", default="utf-8",
                   help="text encoding for RX and TX")
    p.add_argument("--timestamps", action="store_true",
                   help="prefix received lines with a local timestamp")
    p.add_argument("--hexdump", action="store_true",
                   help="show raw bytes of received lines (toggle at runtime with F1)")
    p.add_argument("--list", action="store_true",
                   help="list available serial ports and exit")
    return p.parse_args()


def list_available_ports():
    ports = list(sorted(list_ports.comports(), key=lambda c: c.device))
    if not ports:
        print("No serial ports found. Check the connection and the driver "
              "(Device Manager -> Ports).")
    else:
        print("Available serial ports:")
        for i, info in enumerate(ports, 1):
            desc = f"  {i}: {info.device}"
            if info.description:
                desc += f"  ({info.description})"
            print(desc)
    return ports


def choose_port(requested):
    if requested:
        return requested
    ports = list_available_ports()
    if not ports:
        sys.exit(1)
    if len(ports) == 1:
        print(f"Using the only port: {ports[0].device}")
        return ports[0].device
    while True:
        choice = input("Select a port (1-{}, Ctrl+C to abort): "
                       .format(len(ports))).strip()
        if choice.isdigit() and 1 <= int(choice) <= len(ports):
            return ports[int(choice) - 1].device
        print("Invalid selection, try again.")


def open_port(args):
    try:
        ser = serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=args.bytesize,
            parity=args.parity,
            stopbits=args.stopbits,
            timeout=args.timeout,
        )
    except serial.SerialException as exc:
        msg = str(exc)
        if "could not open port" in msg or "does not exist" in msg:
            sys.exit(f"Port {args.port} does not exist. "
                     "Run with --list to see available ports.")
        if "13" in msg or "access" in msg.lower() or "permission" in msg.lower():
            sys.exit(f"Cannot open {args.port}: access denied. "
                     "Close other programs using this port "
                     "(PuTTY, Arduino Serial Monitor, a second terminal, ...).")
        sys.exit(f"Could not open {args.port}: {msg}")
    return ser


def build_tx_payload(user_input, args):
    """Convert the TX input line into bytes to transmit (None = quit)."""
    if user_input.lower() == r"\quit":
        return None
    if user_input.lower().startswith(r"\hex"):
        try:
            return bytes.fromhex(user_input[4:].replace(" ", "")) + \
                EOL_CHOICES[args.eol]
        except ValueError:
            return b""  # invalid hex: warn in the status area, send nothing
    try:
        return user_input.encode(args.encoding) + EOL_CHOICES[args.eol]
    except UnicodeEncodeError:
        return b""


def reader_loop(ser, args, rx_q, stop_event):
    """Background thread: put every received line into the RX queue."""
    while not stop_event.is_set():
        try:
            line = ser.readline()  # returns b"" on timeout
        except (serial.SerialException, OSError, TypeError):
            if not stop_event.is_set():
                rx_q.put({"text": "[serial error - reader stopped, "
                                  "device disconnected?]", "hex": None})
                stop_event.set()
            return
        if not line:
            continue
        prefix = time.strftime("%H:%M:%S ") if args.timestamps else ""
        rx_q.put({"text": prefix + line.decode(args.encoding,
                                               errors="replace").rstrip("\r\n"),
                  "hex": line})


def visible_rx_lines(entries, width, hexdump, offset, inner_h):
    """Flatten entries into display lines (wrapped), return the visible slice.

    `offset` = number of display lines scrolled up from the tail (0 = follow).
    Returned lines are oldest-first, so the newest line sits at the bottom.
    """
    flat = []
    for e in entries:
        cache = e.get("wrap_cache")
        if cache is None or cache[0] != width:
            lines = []
            text = e["text"]
            while text:
                lines.append(text[:width])
                text = text[width:]
            if hexdump and e.get("hex") is not None:
                hexline = "hex: " + " ".join(f"{b:02X}" for b in e["hex"])
                while hexline:
                    lines.append(hexline[:width])
                    hexline = hexline[width:]
            cache = (width, lines)
            e["wrap_cache"] = cache
        flat.extend(cache[1])
    start = len(flat) - offset - inner_h
    if start < 0:
        start = 0
    return flat[start:start + inner_h]


def tui_main(stdscr, ser, args, rx_q, stop_event):
    curses.curs_set(1)
    stdscr.timeout(100)   # poll getch every 100 ms
    stdscr.keypad(True)

    input_buf = ""
    offset = 0            # scroll offset in display lines from the tail
    hexdump = args.hexdump
    entries = []          # received entries: {"text": str, "hex": bytes|None}

    while not stop_event.is_set():
        # --- input events --------------------------------------------
        quit_flag = False
        while True:
            ch = stdscr.getch()
            if ch == -1:
                break
            if ch in (3, 4):                       # Ctrl+C, Ctrl+D
                quit_flag = True
            elif ch in (10, 13, curses.KEY_ENTER):
                if input_buf:
                    payload = build_tx_payload(input_buf, args)
                    if payload is None:             # \quit
                        quit_flag = True
                    elif input_buf.lower().startswith(r"\hex") and not payload:
                        entries.append({"text": "[invalid hex, nothing sent]",
                                        "hex": None})
                    else:
                        try:
                            ser.write(payload)
                            ser.flush()
                        except (serial.SerialException, OSError):
                            stop_event.set()
                            quit_flag = True
                    input_buf = ""
                    offset = 0
            elif ch in (8, 127, curses.KEY_BACKSPACE):
                input_buf = input_buf[:-1]
            elif ch == curses.KEY_PPAGE:
                offset += 10
            elif ch == curses.KEY_NPAGE:
                offset = max(0, offset - 10)
            elif ch == curses.KEY_END:
                offset = 0
            elif ch == curses.KEY_F0 + 1:
                hexdump = not hexdump
                for e in entries:
                    e["wrap_cache"] = None          # re-wrap with/without hex
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

        status = (f" {args.port} @ {args.baud} "
                  f"{args.bytesize}{args.parity}{args.stopbits:g}"
                  f" | eol={args.eol.upper()}"
                  f" | F1:hex {'on' if hexdump else 'off'}"
                  f" | PgUp/PgDn: scroll")
        stdscr.erase()
        stdscr.addnstr(0, 0, status.ljust(w - 1), w - 1, curses.A_REVERSE)

        # RX pane with border (rows 1 .. h-4)
        rx_h = h - 4
        rx_win = stdscr.derwin(rx_h, w, 1, 0)
        rx_win.border()
        rx_win.addnstr(0, 2, " RX ", 4, curses.A_BOLD)
        inner_h = rx_h - 2
        inner_w = w - 4
        vis = visible_rx_lines(entries, inner_w, hexdump, offset, inner_h)
        for i, txt in enumerate(vis):
            rx_win.addnstr(1 + i, 1, txt, inner_w, curses.A_NORMAL)
        rx_win.refresh()

        # TX input line (row h-3) and hints (row h-2)
        stdscr.addnstr(h - 3, 0, "TX> ", 4, curses.A_BOLD)
        visible_input = input_buf[-(w - 5):]
        stdscr.addnstr(h - 3, 4, visible_input, w - 5, curses.A_NORMAL)
        hints = (" Enter: send | \\hex 41 42: raw bytes"
                 " | End: tail | Ctrl+C: quit")
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
    reader = threading.Thread(
        target=reader_loop, args=(ser, args, rx_q, stop_event), daemon=True)
    reader.start()

    try:
        curses.wrapper(tui_main, ser, args, rx_q, stop_event)
    except KeyboardInterrupt:
        pass  # Ctrl+C: curses.wrapper has already restored the terminal
    finally:
        stop_event.set()
        try:
            ser.close()
        except Exception:
            pass
        reader.join(timeout=2)
    print("Disconnected. Bye.")


if __name__ == "__main__":
    main()