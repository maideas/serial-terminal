"""Shared helpers for the serial-terminal tools.

Used by serial_reader.py, serial_terminal.py and serial_tui.py so that the
command line parsing, port selection, error classification and input/output
sanitising behave identically in all three tools.
"""

import argparse
import codecs
import errno
import re
import sys

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    sys.exit(
        "pyserial is not installed. Windows:  py -m pip install pyserial;  "
        "Linux/macOS:  pip install pyserial"
    )

EOL_CHOICES = {
    "crlf": b"\r\n",  # default for most devices / classic terminals
    "lf": b"\n",
    "cr": b"\r",
    "none": b"",
}

# Upper bound for the number of RX entries the TUI keeps in memory.
RX_HISTORY_MAX = 10_000


# --------------------------------------------------------------------------
# argparse helpers
# --------------------------------------------------------------------------
def positive_int(text):
    """argparse type: integer > 0."""
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an integer: {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {value}")
    return value


def positive_float(text):
    """argparse type: float > 0 (a timeout of 0 would busy-loop)."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not value > 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {text}")
    return value


def encoding_name(text):
    """argparse type: a codec name known to Python."""
    try:
        codecs.lookup(text)
    except LookupError:
        raise argparse.ArgumentTypeError(f"unknown encoding: {text!r}") from None
    return text


def add_common_args(parser, *, tx, default_timeout=1.0):
    """Add the options shared by all tools.

    `tx=True` adds the transmit-related options (--eol, encoding for TX).
    """
    parser.add_argument(
        "-p",
        "--port",
        metavar="PORT",
        help="serial port, e.g. COM3 or /dev/ttyUSB0 (default: interactive "
        "picker, or the only port if there is exactly one)",
    )
    parser.add_argument("-b", "--baud", type=positive_int, default=115200, help="baud rate")
    parser.add_argument(
        "--bytesize", type=int, choices=[5, 6, 7, 8], default=8, help="number of data bits"
    )
    parser.add_argument(
        "--parity",
        choices=list("NEOMS"),
        default="N",
        help="parity: N=none, E=even, O=odd, M=mark, S=space",
    )
    parser.add_argument(
        "--stopbits", type=float, choices=[1, 1.5, 2], default=1, help="number of stop bits"
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=default_timeout,
        help="serial read timeout in seconds (how often the reader wakes up)",
    )
    if tx:
        parser.add_argument(
            "--eol",
            choices=list(EOL_CHOICES),
            default="crlf",
            help="line ending appended to what you send",
        )
    parser.add_argument(
        "--encoding",
        type=encoding_name,
        default="utf-8",
        help="text encoding for RX" + (" and TX" if tx else ""),
    )
    parser.add_argument(
        "--timestamps",
        action="store_true",
        help="prefix each received line with a local timestamp",
    )
    parser.add_argument(
        "--hexdump",
        action="store_true",
        help="also show the raw bytes of each received line as hex",
    )
    parser.add_argument("--list", action="store_true", help="list available serial ports and exit")


# --------------------------------------------------------------------------
# port discovery / opening
# --------------------------------------------------------------------------
def list_available_ports():
    ports = sorted(list_ports.comports(), key=lambda c: c.device)
    if not ports:
        print(
            "No serial ports found. Check the connection and the driver "
            "(Windows: Device Manager -> Ports; Linux: ls /dev/ttyUSB* /dev/ttyACM*)."
        )
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
        try:
            choice = input(f"Select a port (1-{len(ports)}, Ctrl+C to abort): ").strip()
        except EOFError:
            sys.exit(1)
        if choice.isdigit() and 1 <= int(choice) <= len(ports):
            return ports[int(choice) - 1].device
        print("Invalid selection, try again.")


_WINERROR_RE = re.compile(r"WinError (\d+)")
_WINERROR_TO_ERRNO = {
    2: errno.ENOENT,  # ERROR_FILE_NOT_FOUND
    3: errno.ENOENT,  # ERROR_PATH_NOT_FOUND
    5: errno.EACCES,  # ERROR_ACCESS_DENIED (port held by another program)
    32: errno.EBUSY,  # ERROR_SHARING_VIOLATION
}


def classify_open_error(exc):
    """Map a SerialException from Serial() to ('missing' | 'busy' | None).

    pyserial wraps every OSError on POSIX as "could not open port ..." with
    the errno attached; on Windows the message embeds "[WinError N]".
    """
    code = getattr(exc, "errno", None)
    if code is None:
        match = _WINERROR_RE.search(str(exc))
        if match:
            code = _WINERROR_TO_ERRNO.get(int(match.group(1)))
    if code in (errno.ENOENT, errno.ENXIO, errno.ENODEV):
        return "missing"
    if code in (errno.EACCES, errno.EPERM, errno.EBUSY):
        return "busy"
    return None


def open_port(args):
    """Open the serial port described by `args`, exiting with a helpful message on failure."""
    try:
        return serial.Serial(
            port=args.port,
            baudrate=args.baud,
            bytesize=args.bytesize,
            parity=args.parity,
            stopbits=args.stopbits,
            timeout=args.timeout,
        )
    except ValueError as exc:  # pyserial rejects a parameter (e.g. unsupported baud rate)
        sys.exit(f"Invalid serial parameter: {exc}")
    except serial.SerialException as exc:
        kind = classify_open_error(exc)
        if kind == "missing":
            sys.exit(f"Port {args.port} does not exist. Run with --list to see available ports.")
        if kind == "busy":
            sys.exit(
                f"Cannot open {args.port}: access denied or port in use. "
                "Close other programs using this port (PuTTY, Arduino Serial "
                "Monitor, a second terminal, ...) or check the device permissions "
                "(Linux: dialout group)."
            )
        sys.exit(f"Could not open {args.port}: {exc}")


def describe_port(args):
    """'COM3 @ 115200 8N1' style summary for banners / status bars."""
    return f"{args.port} @ {args.baud} {args.bytesize}{args.parity}{args.stopbits:g}"


# --------------------------------------------------------------------------
# RX / TX helpers
# --------------------------------------------------------------------------
def sanitize(text):
    """Make received text safe to print on a terminal.

    Control characters (ESC, BEL, CR, ...) are shown as \\xNN so a device
    cannot inject escape sequences that clear the screen, change the window
    title, or overwrite earlier output. Tabs are kept.
    """
    return "".join(c if c == "\t" or c.isprintable() else f"\\x{ord(c):02x}" for c in text)


def hexdump_line(data):
    return "hex: " + " ".join(f"{b:02X}" for b in data)


def decode_rx_line(line, args):
    """Decode a received line into display text (without EOL, sanitised)."""
    text = line.decode(args.encoding, errors="replace").rstrip("\r\n")
    return sanitize(text)


class InvalidHexError(ValueError):
    """Raised by build_tx_payload for a malformed \\hex command."""


def build_tx_payload(user_input, args):
    r"""Convert one line of user input into the bytes to transmit.

    Returns None for the \quit command. Raises InvalidHexError for a bad
    \hex command. A leading "\\" sends a literal backslash.
    """
    user_input = user_input.rstrip("\r\n")
    eol = EOL_CHOICES[args.eol]
    command, _, rest = user_input.partition(" ")
    lowered = command.lower()
    if lowered == r"\quit" and not rest.strip():
        return None
    if lowered == r"\hex":
        tokens = rest.split()
        if not tokens or any(len(t) != 2 for t in tokens):
            raise InvalidHexError("expected pairs of hex digits, e.g. \\hex 41 42 0D")
        try:
            return bytes.fromhex("".join(tokens)) + eol
        except ValueError:
            raise InvalidHexError("not hex digits, e.g. \\hex 41 42 0D") from None
    if user_input.startswith("\\\\"):
        user_input = user_input[1:]
    return user_input.encode(args.encoding, errors="strict") + eol
