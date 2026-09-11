#!/usr/bin/env python3
"""Interactive terminal for USB-to-UART adapters: read *and* write (RX + TX).

Prints every line the device sends, and sends whatever you type (Enter to
transmit). Kill with Ctrl+C.

Usage examples (Windows):
    py serial_terminal.py                      # interactive port picker, 115200 8N1
    py serial_terminal.py --list
    py serial_terminal.py -p COM3 -b 9600
    py serial_terminal.py -p COM3 --eol lf     # send \\n instead of \\r\\n
    py serial_terminal.py -p COM3 --hexdump --timestamps

Usage examples (Linux/macOS):
    python3 serial_terminal.py -p /dev/ttyUSB0 -b 9600

Special input prefixes:
    \\hex 41 42 0D    send raw bytes given as hex ("AB" -> 0x41 0x42 0x0D),
                     plus the configured line ending (--eol none to omit it)
    \\quit           same as Ctrl+C
    \\\\text          send a line starting with a literal backslash

Requirements:  pyserial, and serial_common.py next to this file
               (or install the package:  pip install .  →  serial-terminal).
"""

import argparse
import contextlib
import sys
import threading
import time

from serial_common import (
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


def parse_args():
    p = argparse.ArgumentParser(
        description="Interactive RX/TX terminal for a USB serial adapter.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_common_args(p, tx=True)
    return p.parse_args()


def reader_loop(ser, args, stop_event, print_lock, error):
    """Background thread: print everything the device sends.

    On failure the reason is stored in `error["reader"]` and stop_event is
    set so the main thread can shut down and report it.
    """
    while not stop_event.is_set():
        try:
            line = ser.readline()  # returns b"" on timeout
            if not line:
                continue
            prefix = time.strftime("%H:%M:%S ") if args.timestamps else ""
            text = prefix + decode_rx_line(line, args)
            with print_lock:
                print(text)
                if args.hexdump:
                    print("    " + hexdump_line(line))
        except Exception as exc:  # a dead RX thread must always be visible
            # SerialException/OSError: adapter unplugged; TypeError: fd
            # closed under us while exiting (POSIX). Anything else is a bug,
            # but must still be reported rather than dying silently.
            if not stop_event.is_set():
                error["reader"] = f"{type(exc).__name__}: {exc}"
                with print_lock:
                    print(
                        "\n[serial error - reader stopped, device disconnected? "
                        "Press Enter to exit]"
                    )
                stop_event.set()
            return


def main():
    args = parse_args()

    if args.list:
        list_available_ports()
        return

    args.port = choose_port(args.port)
    ser = open_port(args)
    ser.reset_input_buffer()  # drop stale bytes buffered before we started

    stop_event = threading.Event()
    print_lock = threading.Lock()
    error = {"reader": None, "writer": None}
    reader = threading.Thread(
        target=reader_loop, args=(ser, args, stop_event, print_lock, error), daemon=True
    )
    reader.start()

    print(
        f"\nConnected to {describe_port(args)} (TX line ending: {args.eol.upper()}).\n"
        "Type a message and press Enter to send, "
        r"'\hex 41 42' for raw bytes, '\quit' or Ctrl+C to quit." + "\n"
    )
    try:
        while not stop_event.is_set():
            try:
                user_input = input("TX> ")
            except EOFError:  # stdin closed (e.g. piped input ended)
                break
            if stop_event.is_set():  # reader died while we were waiting for input
                break
            try:
                payload = build_tx_payload(user_input, args)
            except InvalidHexError as exc:
                with print_lock:
                    print(f"  invalid hex, nothing sent ({exc})")
                continue
            except UnicodeEncodeError as exc:
                with print_lock:
                    print(f"  cannot encode input as {args.encoding}: {exc}")
                continue
            if payload is None:  # \quit
                break
            try:
                ser.write(payload)
                ser.flush()
            except (serial.SerialException, OSError) as exc:
                error["writer"] = str(exc)
                break
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        # let the reader finish its last read first, so we never close
        # the port out from under a blocked readline()
        reader.join(timeout=max(args.timeout, 1.0) + 2)
        with contextlib.suppress(Exception):  # best effort, port may already be gone
            ser.close()
        print()
        if error["writer"]:
            sys.exit(f"Serial write failed (device disconnected?): {error['writer']}")
        if error["reader"]:
            sys.exit(f"Serial read failed (device disconnected?): {error['reader']}")
        print("Disconnected. Bye.")


if __name__ == "__main__":
    main()
