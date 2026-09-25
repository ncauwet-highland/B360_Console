================================================================================
 tools/B360_Console -- the B360 console and its capture tools
================================================================================

Everything here runs on a PC, not on the board: a terminal plus a waveform
viewer that share one connection to the board, shipped as a single Windows
executable that needs nothing installed, and the capture code behind it as
plain source.

Self-contained -- nothing outside this folder is imported, and nothing outside
it imports these. The rest of tools/ is unrelated characterisation and test
scripts.

User-facing documentation:
  ../docs/B360_console_Quickstart.docx   how to use it
  ../docs/b360_console.docx              how it works and why
  b360_console_plan.md                 the design plan it was built from


--------------------------------------------------------------------------------
 THE B360 CONSOLE
--------------------------------------------------------------------------------

Build with:   build_b360_console_exe.bat      ->  dist/b360_console.exe

Source, in dependency order (lower items depend on higher ones):

  b360_link.py
      Transport and reply framing. TCP (standard library only) and USB serial
      (pyserial, imported lazily so its absence cannot break the TCP path).
      Owns one `Board` object with a lock, because the firmware cannot cope with
      two conversations at once. This is where the framing rules live: TCP has
      no line framing, every reply ends "; \r\n", and a WR dump is thousands of
      CRLF-separated rows inside a single reply token.

  b360_capture.py
      The capture sequence -- MO, WC, WG, poll WS to DONE, then WR. Importable
      and also a command-line tool for scripted runs. Reads in 512-sample
      windows and validates each one: the firmware's reply path is deliberately
      non-blocking and does not retry, so a full TX FIFO means a chunk is not
      sent and recovery is the host's job (../docs/b360_console_draftOrig.md).

  b360_console_app.py
      The application. Owns the Board session, creates the two windows, and
      exposes the js_api bridge the pages call. Contains no plotting and no
      protocol code of its own.

  b360_console.html
      The terminal page. Line-oriented on purpose: the device never sends an
      escape sequence and does not echo.

      Not usable on its own. It talks to the board only through
      window.pywebview.api, and no browser can open a TCP or serial socket from
      a file:// page anyway. Opened in a browser it waits 1.5 s for the bridge,
      then says so and disables the controls -- it used to hang on Connect
      instead. api() returns a rejecting stand-in rather than null, so any call
      that slips through reports an error instead of throwing part-way through.

  b360_wave_viewer.html
      The plot, traces, statistics and capture panel. Also works on its own in
      a browser for looking at CSV files -- it just cannot capture there, since
      a browser cannot open a socket. Plot is hand-written canvas; no charting
      library, so the file stays self-contained and auditable.

  b360_console.ico
  b360_console.version.txt
      Icon and Windows version resource. The version resource is not cosmetic:
      a binary with blank Properties -> Details is what an unidentified file
      looks like.

  build_b360_console_exe.bat
      Creates a throwaway venv in %TEMP% (your system Python is untouched),
      builds the one-file executable, runs its self-test, and writes the
      SHA-256 sidecar. Uses pushd, not cd, so it works from a \\wsl.localhost
      path. Needs Python only on the build machine.

  requirements.txt
      For running the console from source instead of the executable. Pins the
      two direct dependencies to the versions the shipped executable was built with
      (pywebview 6.2.1, pyserial 3.5); pip resolves the rest, 10 packages in
      all. Python 3.8 or newer.

          python -m venv .venv
          .venv\Scripts\activate
          pip install -r requirements.txt
          python b360_console_app.py

      b360_console_app.py falls back to its own directory when sys._MEIPASS is
      absent, so the .py and .html files just need to sit together.

      For a site that will not run an outside .exe, the preferred answer is
      b360_capture.py over TCP, not this file: it imports only the standard
      library, with serial imported lazily inside the serial transport, so it
      needs no packages at all. Two files -- b360_capture.py and b360_link.py --
      and the CSV opens in b360_wave_viewer.html in a browser. Verified in a
      venv built with --without-pip and in a folder holding only those two
      files.

Diagnostics that need no board:
  dist\b360_console.exe --selftest     assemble and exit; reports both pages,
                                       the modules, pywebview and pyserial
  dist\b360_console.exe --smoke 5      open both windows, then close them

  The same two flags work on b360_console_app.py when running from source.


--------------------------------------------------------------------------------
 CAPTURE FILE FORMAT
--------------------------------------------------------------------------------

Plain CSV, "time,voltage,comparator", with "#" metadata comment lines above the
header:

  # B360 waveform capture
  # source: tcp://B360-0004.local:5007 (192.168.0.123)
  # sample_rate_hz: 10000
  # samples: 4096 (from address 0)
  # retried_windows: 0
  # captured_utc: 2026-09-18T22:08:14Z
  # voltage is at the ADC pin; the 1k/4.53k divider is not undone
  time,voltage,comparator
  0.000000000,1.1507,0

  time        seconds since the WG that started the capture
  voltage     volts AT THE ADC PIN; the 1k/4.53k divider ahead of it (0.819168)
              is NOT undone
  comparator  0/1 level latched with that sample, for the channel MO selected

The "source" line records the address that answered as well as what was typed.
A <model>-<serial>.local name always means the same board, but its address is a
DHCP lease, and more than one B960 is usually on the network. A "monitor_mux
(MO)" line appears too when --channel was given.

The viewer skips "#" lines, so the metadata costs nothing to anything reading
these files.


--------------------------------------------------------------------------------
 THINGS THAT WILL BITE YOU
--------------------------------------------------------------------------------

The CLI port is 5007, not 5008. 5008 is PORT_UDP6 and has no TCP listener.

The board has one TCP socket. While PuTTY or TeraTerm holds it, nothing else
can connect -- which is why the console is a terminal rather than a capture
button you would run beside one.

Do not use USB and TCP at the same time. cmd_src is a single global set per
command, so traffic on one transport part-way through a reply on the other
reroutes the rest of that reply.

Over USB, send CR alone. read_to_process() terminates on '\r' OR '\n' with no
pair suppression, so a CRLF ends the line twice and draws an extra reply.

A page script must not declare a global named `history`. That is
window.history, a getter-only property; assigning to it under 'use strict'
throws and silently kills the rest of the script.


--------------------------------------------------------------------------------
 ELSEWHERE IN tools/
--------------------------------------------------------------------------------

Not part of the console, and not maintained alongside it:

  ../r360_period_probe.py   Polls the B360 tachometer registers hunting the
                            spurious 2x period. TCP or serial, optional CSV.
                            Standing regression for the chan_in synchroniser.
  ../floor_code_solver.py   Vernier floor-code maths. No board I/O.
  ../old/                   Older characterisation and logging scripts,
                            gitignored and unmaintained. They carry their own
                            pyserial-based connection class rather than using
                            b360_link.py.

House style for anything new here: '#!/usr/bin/env python3', a prose module
docstring reused as the argparse epilog, long-form flags only, no logging
module, sys.exit(2) for usage and connection errors.
