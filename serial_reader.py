#!/usr/bin/env python3
"""Line-based reader for USB-to-UART adapters (works on Windows, Linux, macOS).

Reads from a serial port (COM3 on Windows, /dev/ttyUSB0 on Linux) and prints
every received line. Kill with Ctrl+C.

Usage examples (Windows):
    py serial_reader.py                       # interactive port picker, 115200 8N1
    py serial_reader.py --list                # just list available ports
    py serial_reader.py -p COM3 -b 9600
    py serial_reader.py -p COM3 -b 9600 --parity E --stopbits 2
    py serial_reader.py -p COM3 --timestamps --hexdump

Usage examples (Linux/macOS):
    python3 serial_reader.py -p /dev/ttyUSB0 -b 9600

Requirements:  Windows:  py -m pip install pyserial
               Linux/macOS: pip install pyserial (or: pip3 ...)
"""

import argparse
import sys
import time

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit("pyserial is not installed. Windows:  py -m pip install pyserial;  "
             "Linux/macOS:  pip install pyserial")


def parse_args():
    p = argparse.ArgumentParser(
        description="Read line-based UART data from a USB serial adapter.",
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
                   help="read timeout in seconds (how often Ctrl+C is checked)")
    p.add_argument("--encoding", default="utf-8",
                   help="text encoding of the received data")
    p.add_argument("--timestamps", action="store_true",
                   help="prefix each line with a local timestamp")
    p.add_argument("--hexdump", action="store_true",
                   help="also show the raw bytes of each line as hex")
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
    """Return the port device string, prompting if not given on the CLI."""
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


def main():
    args = parse_args()

    if args.list:
        list_available_ports()
        return

    args.port = choose_port(args.port)
    ser = open_port(args)
    with ser:
        ser.reset_input_buffer()  # drop stale bytes buffered before we started
        print(f"\nReading from {args.port} @ {args.baud} baud, "
              f"{args.bytesize}{args.parity}{args.stopbits:g}, "
              f"press Ctrl+C to stop.\n")
        try:
            while True:
                line = ser.readline()  # returns b"" on timeout
                if not line:
                    continue
                prefix = time.strftime("%H:%M:%S ") if args.timestamps else ""
                text = line.decode(args.encoding, errors="replace")
                print(prefix + text.rstrip("\r\n"))
                if args.hexdump:
                    print("    hex: " + " ".join(f"{b:02X}" for b in line))
        except KeyboardInterrupt:
            print("\nStopped.")
        except serial.SerialException as exc:
            # e.g. the adapter was unplugged while reading
            sys.exit(f"\nSerial error: {exc}")


if __name__ == "__main__":
    main()