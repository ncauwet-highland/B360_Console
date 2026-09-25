#!/usr/bin/env python3
"""Capture a B360 waveform to CSV, without copy-pasting out of a terminal.

Runs the sequence a person would type -- optional `MO`, optional `WC`, then
`WG`, poll `WS` to DONE, and read the RAM with `WR` -- and writes the result as
`time,voltage,comparator` rows that b360_wave_viewer.html, beside it, plots directly.

Two details are not cosmetic:

`WS` must be polled to DONE. Polling is what advances the transaction and
releases the mezzanine SPI slot; skip it and every later R360 command answers
`ERR acquisition in progress`.

The read is done in windows, and each window is checked. The firmware's reply
path is deliberately non-blocking and does not retry: `format_and_send()` ignores
`send()`'s return, so a full 2 KB TX FIFO means that chunk -- four CSV rows -- is
not sent, with no error token and no short count to say so. Recovery is the host's
job, so a short or gappy window is re-read rather than trusted. The FPGA RAM still
holds the samples, so retries cost nothing and the data cannot go stale between
attempts.

Usage:
    b360_capture.py --host 192.168.0.25 --rate 10k --csv cap.csv
    b360_capture.py --host B360-1234.local --channel 0 --rate 750k
    b360_capture.py --serial-port /dev/ttyACM0 --rate 10k
"""

import argparse
import datetime
import os
import sys

try:
    from b360_link import (Board, LinkError, strip_frame, list_serial_ports,
                           DEFAULT_TCP_PORT, DEFAULT_BAUD, EOL_CHOICES,
                           DEFAULT_EOL_TCP, DEFAULT_EOL_SERIAL)
except ImportError:                                   # running from elsewhere
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from b360_link import (Board, LinkError, strip_frame, list_serial_ports,
                           DEFAULT_TCP_PORT, DEFAULT_BAUD, EOL_CHOICES,
                           DEFAULT_EOL_TCP, DEFAULT_EOL_SERIAL)

#: Capture RAM depth, B360_WAVE_DEPTH in include/definitions.h.
WAVE_DEPTH = 4096

#: Rows per `WR`. Small enough to keep the board's 2 KB TX FIFO from being the
#: thing under pressure, large enough that a full read is only eight round trips.
DEFAULT_WINDOW = 512

CSV_HEADER = "time,voltage,comparator"


class CaptureError(Exception):
    """The board answered, but not with a usable capture."""


def parse_rows(body):
    """Turn a `WR` reply body into [(t, volts, comparator)]."""
    rows = []
    for raw in body.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        parts = line.split(",")
        if len(parts) < 3:
            raise CaptureError("malformed row %r" % line)
        try:
            rows.append((float(parts[0]), float(parts[1]), int(parts[2])))
        except ValueError:
            raise CaptureError("unparsable row %r" % line)
    return rows


def check_window(rows, count, step=None, tol=0.02):
    """Return None if the window looks whole, else why it does not.

    A dropped chunk shows up two ways at once -- fewer rows than asked for, and
    a step in the time column that is a multiple of the real one. Both are
    checked, because a drop at the very end only shows as the former.
    """
    if len(rows) != count:
        return "expected %d rows, got %d" % (count, len(rows))
    if len(rows) < 2:
        return None
    steps = [rows[i][0] - rows[i - 1][0] for i in range(1, len(rows))]
    observed = sorted(steps)[len(steps) // 2]
    if observed <= 0:
        return "time column does not advance"
    ref = step if step else observed
    for i, s in enumerate(steps):
        if abs(s - ref) > tol * ref:
            return ("time gap at row %d: step %.9g s, expected %.9g s"
                    % (i + 1, s, ref))
    return None


def read_window(board, start, count, step=None, retries=2, timeout=30.0,
                echo=None):
    """Read one window of samples, re-reading it if it arrives damaged."""
    problem = None
    for attempt in range(retries + 1):
        # A dump always ends in the "; " token, so the terminator is what really
        # finishes this read; the idle gap is only a fallback and is widened so a
        # stall part-way through a 90 KB transfer cannot truncate it.
        cmd = "WR %d %d" % (start, count)
        if echo:
            echo(cmd)
        body = strip_frame(board.command(cmd, timeout=timeout, idle_gap=1.0))
        if body.upper().startswith("ERR"):
            raise CaptureError("board rejected WR %d %d: %s" % (start, count, body))
        try:
            rows = parse_rows(body)
        except CaptureError as exc:
            problem = str(exc)
            continue
        problem = check_window(rows, count, step)
        if problem is None:
            return rows, attempt
    raise CaptureError("window at %d still bad after %d attempts: %s"
                       % (start, retries + 1, problem))


def wait_done(board, poll_timeout, progress=None, echo=None):
    """Poll `WS` until the acquisition finishes.

    Not optional, and not a passive read: polling is what advances the
    transaction and releases the SPI slot.
    """
    import time
    deadline = time.monotonic() + poll_timeout
    last = ""
    while time.monotonic() < deadline:
        if echo:
            echo("WS")
        last = strip_frame(board.command("WS")).upper()
        if last.startswith("DONE"):
            return
        if last.startswith("FAIL"):
            raise CaptureError("acquisition failed: %s" % last)
        if last.startswith("IDLE"):
            raise CaptureError("board reports IDLE -- WG did not start an acquisition")
        if progress:
            progress("waiting for acquisition (%s)" % last.lower())
        time.sleep(0.05)
    raise CaptureError("acquisition did not finish within %.1f s (last status %s)"
                       % (poll_timeout, last or "none"))


def capture(board, rate_hz=None, channel=None, start=0, count=WAVE_DEPTH,
            window=DEFAULT_WINDOW, retries=2, progress=None, metadata=True,
            echo=None):
    """Run a full capture and return CSV text.

    `rate_hz` and `channel` are optional: leave them out to capture with
    whatever `WC` and `MO` are already set to.

    `echo(line)` is called with every command line before it is sent, so a caller
    can show exactly what went to the board -- these are the only commands the
    app issues that nobody typed.
    """
    def say_cmd(line):
        if echo:
            echo(line)
    if start < 0 or start >= WAVE_DEPTH:
        raise CaptureError("start must be 0-%d" % (WAVE_DEPTH - 1))
    count = min(count, WAVE_DEPTH - start)
    if count < 1:
        raise CaptureError("count must be 1 or more")

    def say(msg):
        if progress:
            progress(msg)

    if channel is not None:
        say_cmd("MO %s" % channel)
        reply = strip_frame(board.command("MO %s" % channel))
        if reply.upper().startswith("ERR"):
            raise CaptureError("MO %s rejected: %s" % (channel, reply))

    if rate_hz is not None:
        say_cmd("WC %s" % rate_hz)
        reply = strip_frame(board.command("WC %s" % rate_hz))
        if reply.upper().startswith("ERR"):
            raise CaptureError("WC %s rejected: %s" % (rate_hz, reply))

    say_cmd("WC")
    actual_rate = strip_frame(board.command("WC"))
    try:
        rate_val = float(actual_rate)
    except ValueError:
        raise CaptureError("could not read back the sample rate: %r" % actual_rate)

    say("starting acquisition at %g Hz" % rate_val)
    say_cmd("WG")
    reply = strip_frame(board.command("WG"))
    if reply.upper().startswith("ERR"):
        raise CaptureError("WG rejected: %s" % reply)

    # Worst case is the whole RAM at the slowest rate; give it room plus margin.
    wait_done(board, poll_timeout=(WAVE_DEPTH / max(rate_val, 1.0)) * 2.0 + 5.0,
              progress=progress, echo=echo)
    say("acquisition done, reading %d samples" % count)

    rows, step, retried, pos = [], None, 0, start
    while pos < start + count:
        n = min(window, start + count - pos)
        got, attempts = read_window(board, pos, n, step, retries=retries,
                                    echo=echo)
        retried += attempts
        if step is None and len(got) > 1:
            step = sorted(got[i][0] - got[i - 1][0]
                          for i in range(1, len(got)))[len(got) // 2]
        if rows and step:
            gap = got[0][0] - rows[-1][0]
            if abs(gap - step) > 0.02 * step:
                raise CaptureError(
                    "discontinuity between windows at sample %d: %.9g s, expected %.9g s"
                    % (pos, gap, step))
        rows.extend(got)
        pos += n
        say("read %d/%d samples" % (len(rows), count))

    if retried:
        say("NOTE: %d window read(s) had to be retried" % retried)

    out = []
    if metadata:
        out.append("# B360 waveform capture")
        out.append("# source: %s" % board.name)
        out.append("# sample_rate_hz: %g" % rate_val)
        if channel is not None:
            out.append("# monitor_mux (MO): %s" % channel)
        out.append("# samples: %d (from address %d)" % (len(rows), start))
        out.append("# retried_windows: %d" % retried)
        out.append("# captured_utc: %s"
                   % datetime.datetime.now(datetime.timezone.utc)
                   .strftime("%Y-%m-%dT%H:%M:%SZ"))
        out.append("# voltage is at the ADC pin; the 1k/4.53k divider is not undone")
    out.append(CSV_HEADER)
    for t, v, c in rows:
        out.append("%.9f,%.4f,%d" % (t, v, c))
    return "\n".join(out) + "\n"


def default_name():
    return "b360_capture_%s.csv" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Capture a B360 waveform to CSV over TCP or USB serial.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    ap.add_argument("--host", help="board address or <model>-<serial>.local")
    ap.add_argument("--port", type=int, default=DEFAULT_TCP_PORT,
                    help="TCP command port (default %d)" % DEFAULT_TCP_PORT)
    ap.add_argument("--serial-port", help="USB CDC port instead of TCP, e.g. COM5")
    ap.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    ap.add_argument("--eol", choices=sorted(EOL_CHOICES),
                    help="line ending, as if hitting Enter in a terminal "
                         "(default %s for TCP, %s for serial: over USB a CRLF "
                         "terminates twice and draws an extra reply)"
                         % (DEFAULT_EOL_TCP, DEFAULT_EOL_SERIAL))
    ap.add_argument("--list-serial", action="store_true",
                    help="list candidate serial ports and exit")
    ap.add_argument("--rate", help="sample rate, 1k-750k, SI suffixes accepted")
    ap.add_argument("--channel", help="monitor mux input (MO) to capture")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--count", type=int, default=WAVE_DEPTH)
    ap.add_argument("--window", type=int, default=DEFAULT_WINDOW,
                    help="samples per WR (default %d)" % DEFAULT_WINDOW)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--csv", help="output path (default b360_capture_<stamp>.csv)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    if args.list_serial:
        ports = list_serial_ports()
        if not ports:
            print("no serial ports found (or pyserial is not installed)")
        for p in ports:
            print("%-20s %s%s" % (p["device"], p["description"],
                                  "  [RP2040]" if p["rp2040"] else ""))
        return 0

    if not args.host and not args.serial_port:
        ap.error("need --host or --serial-port")

    progress = None if args.quiet else (lambda m: print(m, file=sys.stderr))

    board = None
    try:
        if args.serial_port:
            board = Board.serial(args.serial_port, args.baud,
                                 eol=args.eol or DEFAULT_EOL_SERIAL)
        else:
            board = Board.tcp(args.host, args.port,
                              eol=args.eol or DEFAULT_EOL_TCP)
        if not args.quiet:
            print("connected to %s" % board.name, file=sys.stderr)
        text = capture(board, rate_hz=args.rate, channel=args.channel,
                       start=args.start, count=args.count, window=args.window,
                       retries=args.retries, progress=progress)
    except LinkError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2
    except CaptureError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        if board:
            board.close()

    path = args.csv or default_name()
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    rows = text.count("\n") - text.count("\n#") - 1
    print("wrote %s (%d rows)" % (path, rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
