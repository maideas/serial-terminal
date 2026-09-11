#!/usr/bin/env python3
"""Interactive terminal for USB-to-UART adapters: read *and* write (RX + TX).

Prints every line the device sends, and sends whatever you type (Enter to
transmit). Kill with Ctrl+C.

Usage examples:
    py serial_terminal.py                      # interactive port picker, 115200 8N1
    py serial_terminal.py --list
    py serial_terminal.py -p COM3 -b 9600
    py serial_terminal.py -p COM3 --eol lf     # send \\n instead of \\r\\n
    py serial_terminal.py -p COM3 --hexdump --timestamps

Special input prefixes:
    \\hex 41 42 0D    send raw bytes given as hex ("AB" -> 0x41 0x42 0x0D),
                     plus the configured line ending (--eol none to omit it)
    \\quit           same as Ctrl+C

Requirements:  py -m pip install pyserial
"""

import argparse
import threading
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is not installed. Run:  pip install pyserial  "
             "(or:  py -m pip install pyserial)")

EOL_CHOICES = {
    "crlf": b"\r\n",   # default for most devices / classic terminals
    "lf":   b"\n",
    "cr":   b"\r",
    "none": b"",
}


def parse_args():
    p = argparse.ArgumentParser(
        description="Interactive RX/TX terminal for a USB serial adapter.",
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
    p.add_argument("--timeout", type=float, default=1.0,
                   help="read timeout in seconds")
    p.add_argument("--eol", choices=list(EOL_CHOICES), default="crlf",
                   help="line ending appended to what you send")
    p.add_argument("--encoding", default="utf-8",
                   help="text encoding for RX and TX")
    p.add_argument("--timestamps", action="store_true",
                   help="prefix each received line with a local timestamp")
    p.add_argument("--hexdump", action="store_true",
                   help="also show the raw bytes of each received line as hex")
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
    """Convert one line of user input into the bytes to transmit."""
    user_input = user_input.strip("\n")
    if user_input.lower() == r"\quit":
        return None
    if user_input.lower().startswith(r"\hex"):
        try:
            return bytes.fromhex(user_input[4:].replace(" ", "")) + EOL_CHOICES[args.eol]
        except ValueError:
            print("  invalid hex, nothing sent "
                  "(example: \\hex 41 42 0D)")
            return b""
    try:
        return user_input.encode(args.encoding) + EOL_CHOICES[args.eol]
    except UnicodeEncodeError as exc:
        print(f"  cannot encode input as {args.encoding}: {exc}")
        return b""


def reader_loop(ser, args, stop_event, print_lock):
    """Background thread: print everything the device sends."""
    while not stop_event.is_set():
        try:
            line = ser.readline()  # returns b"" on timeout
        except (serial.SerialException, OSError, TypeError):
            # TypeError: fd already closed under us while exiting (POSIX)
            if not stop_event.is_set():
                # e.g. adapter unplugged, or port closed while exiting
                with print_lock:
                    print("\n[serial error - reader stopped]")
                stop_event.set()
            return
        if not line:
            continue
        prefix = time.strftime("%H:%M:%S ") if args.timestamps else ""
        text = line.decode(args.encoding, errors="replace")
        with print_lock:
            print(prefix + text.rstrip("\r\n"))
            if args.hexdump:
                print("    hex: " + " ".join(f"{b:02X}" for b in line))


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
    reader = threading.Thread(
        target=reader_loop, args=(ser, args, stop_event, print_lock), daemon=True)
    reader.start()

    print(f"\nConnected to {args.port} @ {args.baud} baud, "
          f"{args.bytesize}{args.parity}{args.stopbits:g} "
          f"(TX line ending: {args.eol.upper()}).\n"
          "Type a message and press Enter to send, "
          r"'\hex 41 42' for raw bytes, Ctrl+C to quit." + "\n")
    try:
        while not stop_event.is_set():
            try:
                user_input = input("TX> ")
            except EOFError:  # stdin closed (e.g. piped input ended)
                break
            payload = build_tx_payload(user_input, args)
            if payload is None:  # \quit
                break
            if payload:
                ser.write(payload)
                ser.flush()
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()
        # let the reader finish its last read first, so we never close
        # the port out from under a blocked readline()
        reader.join(timeout=max(args.timeout, 1.0) + 2)
        try:
            ser.close()
        except Exception:
            pass
        print("\nDisconnected. Bye.")


if __name__ == "__main__":
    main()