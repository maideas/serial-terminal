# serial-terminal

Three Python tools for talking to devices over a USB-to-UART adapter
(FT232, CP2102, CH340, PL2303, ...), built on
[pyserial](https://pyserial.readthedocs.io/).

| Tool | RX | TX | Interface |
|------|----|----|-----------|
| [`serial_tui.py`](serial_tui.py) | line-based | text + raw hex | split-screen curses TUI |
| [`serial_terminal.py`](serial_terminal.py) | line-based | text + raw hex | plain terminal, `TX>` prompt |
| [`serial_reader.py`](serial_reader.py) | line-based | — | plain terminal, print only |

Each tool is a single script you can run directly; they share the CLI
parsing, port selection and RX/TX helpers in
[`serial_common.py`](serial_common.py), so copy a tool together with
that module.

All three share the same conventions:

- **Port selection** — pass `-p COM3` (Windows) or `-p /dev/ttyUSB0`
  (Linux), or run without `-p` and pick from a numbered list of
  detected ports (if there is exactly one, it is used automatically).
  `--list` just prints the ports and exits.
- **Serial parameters** — `-b`/`--baud` (default 115200), plus
  `--bytesize`, `--parity`, `--stopbits` for the frame format
  (default 8N1).
- **Display options** — `--timestamps`, `--hexdump`, `--encoding`.
  Invalid values (`--encoding bogus`, `--timeout 0`, a negative baud
  rate) are rejected by the argument parser.
- **Safe output** — received bytes are never forwarded to your
  terminal verbatim: control characters are shown as `\xNN`, so a
  device (or line noise at the wrong baud rate) cannot clear your
  screen, retitle the window or overwrite earlier output with escape
  sequences.
- **Clean exit** — Ctrl+C always closes the port properly; a missing
  port and a port held by another program (PuTTY, Arduino Serial
  Monitor, ...) are told apart by errno and produce a helpful message
  instead of a traceback. A device unplugged mid-session ends the
  session with the reason on stderr and exit code 1.

## Requirements

- Python 3.8+
- [pyserial](https://pypi.org/project/pyserial/)

  ```bash
  pip install pyserial
  ```

- For [`serial_tui.py`](serial_tui.py) on **Windows** additionally:

  ```bash
  pip install windows-curses
  ```

  (Linux and macOS have curses built in.)

Pinned versions live in [`requirements.txt`](requirements.txt)
(`pip install -r requirements.txt`).

## Development

[`Makefile`](Makefile) targets (needs `ruff`, `pytest` and `pip-audit`
on `PATH`):

```bash
make check     # ruff check + ruff format --check + pytest
make test      # unit tests and pty-based integration tests
make audit     # pip-audit against requirements.txt
make format    # apply ruff formatting and safe fixes
```

The tests need no hardware: [`tests/`](tests) fakes the adapter with a
pty and drives the TUI in a pty-backed terminal
([`tests/smoke_tui.py`](tests/smoke_tui.py)).

## serial_tui.py — split-screen TUI (RX and TX never mix)

An interactive terminal where received data and your typing live in
**separate screen regions**: incoming lines can never interleave with
what you are typing.

```
 COM3 @ 115200 8N1 | eol=CRLF | F1:hex off | PgUp/PgDn: scroll
┌─ RX ────────────────────────────────────────────────────────────
│ 20:30:58 telemetry line 0
│ 20:30:59 TEMP=21.7C
│ 20:30:59 NAK 41 42
└─────────────────────────────────────────────────────────────────
TX> GET TEMP_
 Enter: send | \hex 41 42: raw bytes | End: tail | Ctrl+C: quit
```

Features:

- Bordered **RX pane** that scrolls automatically (follows the tail);
  scroll back with PgUp, forward with PgDn, `End` jumps back to the tail.
- Dedicated **TX input line** with a live cursor at the bottom.
- **F1** toggles a hex dump of received lines at runtime.
- Background reader thread: RX keeps updating while you type.
- Status bar showing port, baud, frame format, EOL and hex state.
- Bounded scrollback (the newest 10000 lines, `RX_HISTORY_MAX` in
  [`serial_common.py`](serial_common.py)), so a session left running
  for days cannot exhaust memory. Pipe
  [`serial_reader.py`](serial_reader.py) to a file if you need a full
  log.

```bash
py serial_tui.py                      # pick port, 115200 8N1
py serial_tui.py -p COM3 -b 9600
py serial_tui.py -p COM3 --eol lf --hexdump --timestamps
```

| Key | Action |
|-----|--------|
| printable ASCII / Backspace | edit the TX line (the TUI input line is ASCII-only; `--encoding` applies to RX and to `\hex`) |
| Enter | send the TX line |
| `\hex 41 42` + Enter | send raw bytes (plus configured EOL) |
| `\\text` + Enter | send a line starting with a literal backslash |
| `\quit` + Enter | quit |
| F1 | toggle hex dump |
| PgUp / PgDn | scroll the RX pane |
| End | jump back to the newest line |
| Ctrl+C | quit |

## serial_terminal.py — interactive RX/TX in a plain terminal

The same read/write capability without curses: received lines are
printed as they arrive, and whatever you type at the `TX>` prompt is
sent on Enter. Use it when you want to pipe/redirect output, run it
inside tmux or over ssh, or simply prefer a no-frills console.

```bash
py serial_terminal.py                # pick port, 115200 8N1, CRLF on send
py serial_terminal.py -p COM3 -b 9600
py serial_terminal.py -p COM3 --eol lf
py serial_terminal.py -p COM3 --timestamps --hexdump
```

- Background reader thread — you can send anytime; RX printing
  continues while you type. (Lines may visually interleave around the
  `TX>` prompt in the plain console; use
  [`serial_tui.py`](serial_tui.py) if that bothers you.)
- `--eol` controls the line ending appended to transmissions
  (`crlf` default, or `lf`/`cr`/`none`).
- Input prefixes: `\hex 41 42` sends raw bytes (plus the configured
  EOL, hex digits in pairs), `\quit` exits — both in addition to
  Ctrl+C. To send a line that really starts with a backslash, double
  it: `\\hex` transmits `\hex`.

## serial_reader.py — read-only monitor

Prints everything the device sends, line-based, and transmits nothing.
Handy for monitoring a device's output (sensor telemetry, logs, debug
traces) without any risk of interfering with it.

```bash
py serial_reader.py                  # pick port, 115200 8N1
py serial_reader.py -p COM3 -b 9600
py serial_reader.py -p COM3 --timestamps      # timestamped lines
py serial_reader.py -p COM3 --hexdump         # + raw bytes as hex
```

## Troubleshooting

- **No ports found** — check Device Manager → *Ports (COM & LPT)* on
  Windows (`ls /dev/ttyUSB*` on Linux); CH340/Prolific adapters often
  need a driver first.
- **Access denied or port in use** — another program is holding the
  port open; close PuTTY, the Arduino Serial Monitor, or a second
  terminal running one of these tools. On Linux this message also
  means your user is not in the `dialout` group
  (`sudo usermod -aG dialout $USER`, then log out and back in).
- **Garbage output** — wrong baud rate (try 9600 and 115200 first);
  expect `\xNN` escapes in the output, since control bytes are shown
  escaped rather than sent to your terminal;
  also verify TX/RX are crossed (adapter TX → device RX) and ground is
  common.
- **Nothing arrives** — loop the adapter's TX to RX and use
  `serial_terminal.py`: what you type should come back.