#!/usr/bin/env python3
"""Line-based reader for USB-to-UART adapters (works on Windows, Linux, macOS).

Reads from a serial port (COM3 on Windows, /dev/ttyUSB0 on Linux) and prints
every received line. Transmits nothing. Kill with Ctrl+C.

Usage examples (Windows):
    py serial_reader.py                       # interactive port picker, 115200 8N1
    py serial_reader.py --list                # just list available ports
    py serial_reader.py -p COM3 -b 9600
    py serial_reader.py -p COM3 -b 9600 --parity E --stopbits 2
    py serial_reader.py -p COM3 --timestamps --hexdump

Usage examples (Linux/macOS):
    python3 serial_reader.py -p /dev/ttyUSB0 -b 9600

Requirements:  pyserial, and serial_common.py next to this file
               (or install the package:  pip install .  →  serial-reader).
"""

import argparse
import sys
import time

from serial_common import (
    add_common_args,
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
        description="Read line-based UART data from a USB serial adapter.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    add_common_args(p, tx=False)
    return p.parse_args()


def main():
    args = parse_args()

    if args.list:
        list_available_ports()
        return

    args.port = choose_port(args.port)
    ser = open_port(args)
    with ser:
        ser.reset_input_buffer()  # drop stale bytes buffered before we started
        print(f"\nReading from {describe_port(args)}, press Ctrl+C to stop.\n")
        try:
            while True:
                line = ser.readline()  # returns b"" on timeout
                if not line:
                    continue
                prefix = time.strftime("%H:%M:%S ") if args.timestamps else ""
                print(prefix + decode_rx_line(line, args))
                if args.hexdump:
                    print("    " + hexdump_line(line))
        except KeyboardInterrupt:
            print("\nStopped.")
        except (serial.SerialException, OSError) as exc:
            # e.g. the adapter was unplugged while reading
            sys.exit(f"\nSerial error (device disconnected?): {exc}")


if __name__ == "__main__":
    main()
