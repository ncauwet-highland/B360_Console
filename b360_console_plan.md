<!--
  Design plan and decision record for the B360 console, kept with the source it
  produced.

  Two parts, deliberately not merged:

    1. The plan as approved on 2026-09-17, preserved rather than rewritten.
    2. "Design record" -- the trail of decisions that produced that plan and then
       changed it, each with what was asked for and what it resulted in.

  The second part is the point of keeping this file: it is the record of what was
  prompted into the final design, including the things that were tried and
  rejected, which would otherwise get re-proposed.

  Paths in the preserved plan read "tools/<file>". The console later moved into
  tools/B360_Console/ to keep it apart from the unrelated characterisation
  scripts; the plan text is left as it was written rather than retro-edited.
-->

# B360 console: terminal + waveform capture in one app

## Context

Getting a capture off the board today means typing `WC`/`WG`/`WS`/`WR` into
TeraTerm or PuTTY and copy-pasting 4096 CSV lines out of the scroll-back.

The RP2040 cannot write a file: no filesystem, no XMODEM/base64/hex path, no USB
mass storage, and the only writable storage is a 2 KB CAL EEPROM against a ~90 KB
capture. Confirmed by search — none of that exists. It does not need to: the
samples sit in FPGA RAM and are re-readable at will, and a full read is only
**90,116 bytes**, about 7 ms of wire time. Nothing is slow; the bottleneck is the
human step.

A capture-button-only tool does not work, because **the board has exactly one TCP
socket** (socket 6, no accept/backlog). While PuTTY holds it, nothing else can
connect. Capturing over USB instead looks like a dodge but is unsafe: `cmd_src`
is a single global (`globals.c:64`) set per-command in `process_commands()`
(`custom_cmd.c:384-388`), so a keystroke in PuTTY mid-capture reroutes the rest of
the CSV to `printf`. `WR` is 4096 blocking SPI transactions, so that window is
seconds wide. Test CL-16 exists for this.

**So one client must own the link.** The app becomes the terminal, and both
contention and the `cmd_src` race become structurally impossible rather than
rules to remember.

## Shape

Two windows, one connection (pywebview multi-window is confirmed available in the
bundled 6.2.1: `webview.windows`, `create_window` after `start()`, per-window
`evaluate_js`/`show`/`hide`).

- **Main window — terminal.** Full height, nothing competing for space.
- **Pop-up window — plot.** The existing viewer page plus the traces list, stats
  and the capture panel. Opened on demand or automatically on the first capture;
  hidden rather than destroyed so loaded traces survive being closed and reopened.

Entry point becomes `b360_console.exe` — "wave viewer" no longer describes a tool
whose main window is a terminal. Trivially revertible if you would rather keep the
old name.

## Terminal: line-oriented, not VT100

The device never emits an escape sequence; replies are plain text with CRLF, and
the firmware does not echo at all — `loopback.c` says so and directs clients to
use local echo. A VT100 emulator's screen-grid machinery would sit entirely
unexercised.

- Input box with history (up/down), local echo, scrollback capped around 5000
  lines, copy/paste from the host.
- Honour a bare `\r` as an overwrite rather than stacking lines, and strip stray
  escape bytes instead of printing mojibake. That is the whole of the terminal
  emulation needed.
- A command typed by hand that returns CSV (`WR`) is offered to the plot window,
  which is the "update live when a waveform is requested" behaviour — a callback,
  not new infrastructure.

## Board facts the implementation must respect

- **TCP port 5007**, socket 6 (`include/wiznet_main.h:68,86`). Not 5008 — that is
  `PORT_UDP6`. `tools/r360_period_probe.py:50` and `docs/wiki.md:522` have it
  wrong; `docs/R360_test_plan.md:415` already records this.
- **No line framing on TCP** (`loopback.c:118-127`): whatever is in the RX FIFO on
  a poll pass becomes one command line. One command per write, read its reply
  before writing again.
- **`WR` reply layout:** leading `\r\n`, 20-char rows separated by `\r\n` (the
  *last* row has no trailing CRLF), then `"; "` appended directly to that last
  row, then `\r\n`. Read until **`"; \r\n"`**. Chunks split mid-row — reassemble
  across `recv()` boundaries. Do **not** reuse `r360_period_probe.py`'s
  ends-on-CRLF rule; a dump is full of CRLFs and it would stop on row one.
- **The reply path does not retry, by design.** `format_and_send()` ignores
  `send()`'s return (`helper_funcs.c:2078`) on a non-blocking socket; a full 2 KB
  TX FIFO returns `SOCK_BUSY` *before* writing, so those 4 rows are not sent and
  nothing reports it. Blocking there would stall the command loop, so recovery is
  the host's job.
- **`WS` polling is mandatory** — it advances the transaction and releases the SPI
  slot (`custom_cmd.c:3379`). Skip it and other R360 commands return
  `ERR acquisition in progress`.
- Sequence: `WC <hz>` → `WG` → poll `WS` until `DONE` → `WR <start> <count>`.
  `MO` picks the channel. `WR`'s time axis uses the divisor latched at `WG`.

UDP is not an option: command dispatch in `loopback_udps()` is commented out
(`loopback.c:573-577`) and the datalog sender under it is disabled. It receives a
datagram and discards it. Using it means building a firmware data path *and* an
application-level retransmit layer — work TCP already does correctly.

## Files

**`tools/b360_link.py`** — transport + session. Stdlib `socket`/`select` for TCP;
`pyserial` imported lazily so USB is optional and never breaks the TCP path. (The
existing `SerialTransport` at `r360_period_probe.py:98-144` is `termios`-based and
cannot run on Windows, so it is not reusable here.) Reuse that file's
drain-before-write discipline, whose docstring at `:147` records why it exists.
One `Board` object guarded by a lock, so typed commands and captures serialize
against each other and can never interleave.

**`tools/b360_capture.py`** — the capture sequence, importable *and* a CLI for
scripted runs. Reads in windows (`WR <start> 512` × 8) rather than one 4096-row
blast; after each window it validates the row count and that the time column
advances by the expected constant step, re-fetching any short or gappy window.
The FPGA RAM is intact, so retries are free. A retried window is reported, not
hidden. Emits `#` metadata lines — host, serial, rate, channel, UTC timestamp —
because more than one B960 is live on the lab network.

**`tools/b360_console.html`** — the terminal page.

**`tools/b360_wave_viewer.html`** — gains the capture panel, a per-trace **Save
CSV** button, and `#`-comment tolerance in `parseCsv` (it currently treats only
the *first* non-numeric line as a header, so metadata lines would count as
skipped rows and raise a warning).

**`tools/b360_wave_viewer_app.py`** → **`b360_console_app.py`**: owns the session,
creates both windows, exposes `js_api`, pushes captured CSV into the plot window
via `evaluate_js`.

Sharing these modules does **not** weaken the standalone EXE — PyInstaller
compiles them in, exactly as it already does the HTML. The recipient still
installs nothing. Bundle `pyserial` so USB works from the EXE.

House style: `#!/usr/bin/env python3`, prose module docstring reused as the
argparse epilog, long flags only, no `logging`, `sys.exit(2)` for usage and
connection errors, `KeyboardInterrupt` handled in a `finally` that closes the
transport.

## Not in this change

`format_and_send()` should check `send()`'s return and retry on `SOCK_BUSY`.
Worth fixing, but it touches every command's reply path, and the windowed
validation above makes capture reliable against firmware already in the field.
Raise separately. Likewise leave `r360_period_probe.py`'s stale 5008 default and
the swapped pair in `docs/wiki.md:522,528` alone — both pre-existing and recorded.

## Verification

Needs a live R360 on the bench.

1. **The contention case, which is the whole point.** Open PuTTY on 5007, then
   launch the console and confirm it reports the port busy clearly. Close PuTTY,
   reconnect, and confirm the terminal works — then never need PuTTY again.
2. Terminal basics: `ID`, a multi-command line (`MO 0; WC 10k; WG`), history,
   local echo, an error path (`WC 999` → `ERR WC rate must be 1000-750000 Hz;`).
3. `./tools/b360_capture.py --host <ip-or-B960-xxxx.local> --rate 10k --csv /tmp/a.csv`
   → 4096 rows, monotonic time, 0.4095 s span. Compare against
   `WaveformCap_4Vsine_400Hz_10kHzSample.csv`, a known-good capture of that shape.
4. Point it at 5008 deliberately; confirm a clear failure rather than a hang.
5. Capture from the panel: plot window pops up, trace appears, CSV lands on disk.
   Close the plot window and capture again — traces from before must still be
   there, proving hide-not-destroy.
6. Type `WR 0 4096` by hand in the terminal and confirm it offers to plot.
7. **`WS` discipline:** run a capture, then immediately send a normal R360
   command and confirm it is *not* rejected with `ERR acquisition in progress` —
   proving `WS` was polled to completion and the SPI slot released.
8. **Loss path:** capture while hammering the link and confirm the tool reports a
   retried window rather than silently returning a short file; verify the retry
   yields a complete, monotonic CSV.
9. Cross-check a known-frequency source: comparator frequency in the viewer
   against `<ch>P` from the same board.
10. Rebuild (`tools\build_b360_viewer_exe.bat`), and on a machine with no Python
    confirm terminal + capture both work. `--selftest` still assembles.
11. `tools/b360_wave_viewer.html` opened directly in a browser still works and
    simply shows no capture panel (a browser cannot open a TCP socket).

---

# Design record

Why this looks the way it does. The plan above is the approved design; this is the
trail of decisions that produced it and then changed it. Kept because the
reasoning is what is expensive to recover, and because several of these were
deliberate rejections that would otherwise get re-proposed.

## Decisions that shaped the plan before it was approved

| Asked for | Outcome |
|---|---|
| A waveform viewer needing **zero setup** — "standalone EXE or equivalent, user would not need to install python or any libraries" | Single self-contained HTML first, then a frozen executable. No charting library, no CDN, no external reference of any kind. |
| Comparator traces **colour-coded and overlapping**, "this avoids the squished look when multiple waveforms are present" | One shared comparator lane rather than one per trace, so the voltage plot keeps its height. Traces are offset a couple of pixels inside it so two at the same level cannot hide each other. |
| **User-selectable trace colours** — "some people may want to colour-code theirs customly" | A colour picker per trace; the waveform, its comparator trace and its statistics block all follow, live. |
| A way to **name pasted data**, and a **title block** above the graph | Name field beside the paste box; plot title drawn on the canvas so it lands in Save PNG. |
| **B360, not R360**, in anything user-facing — "B360 is the user-facing name, R360 is internal, name of the daughter board" | All filenames, window titles and the PNG name use B360. `R360` survives only where it genuinely means the mezzanine. |
| The exe should **run standalone, not open a webpage** — "that was the goal" | pywebview driving the WebView2 control already present in Windows. The browser fallback was dropped for the console, since the JS↔Python bridge is essential to it. |
| Concern: **"if the socket is already open on a terminal the socket will be busy when the EXE tries to pull the data"** | This is the decision the whole shape rests on. Investigation confirmed worse than "busy": one hardware socket with no accept backlog, *and* `cmd_src` is a single global, so capturing over USB beside a terminal corrupts transfers rather than avoiding the problem. |
| Suggestion: **"stop relying on third party terminals and build a TCP terminal interface for the B360 that also does waveform extraction/visualization"** | Chosen over a capture-only tool. Owning the link makes both failures structurally impossible instead of rules to remember. |
| Suggestion: a page that **updates live when a waveform is requested** | Folded into the above rather than built separately — once the app owns the connection this is a callback, not infrastructure. |
| **"waveform plot show in a pop-up rather than taking up valuable terminal space"**, with traces/stats/capture in that same window | Two windows: terminal, and a waveform window holding plot + traces + statistics + capture. Closing the waveform window hides it so loaded traces survive. |
| Asked for the **difference between line-oriented and full VT100** before choosing | Line-oriented. The device never emits an escape sequence and does not echo, so an emulator's screen-grid machinery would sit unexercised. |

## Changes requested after the plan was approved

| Asked for | Outcome |
|---|---|
| **"The default port should be 5007 since that's almost always going to be the port"** | It already was throughout the new tooling; the holdout was `r360_period_probe.py`, defaulting to 5008 — which is `PORT_UDP6`, with no TCP listener, so its `--mode tcp` default could never have connected. Fixed, and the stale port references in the docs were swept. `wiki.md`'s socket table turned out to be shifted throughout, not merely wrong on the port, and was rewritten against `SOCKET_*`/`PORT_*`. |
| The port's hover note **should state what the field is** — "it should say TCP is 5007 if anything" | Reworded from warning about 5008 to `TCP command port (5007)`. |
| **Allow CR+LF, CR or LF** — "equivalent to hitting enter in Putty or TeraTerm… the firmware identifies the device when it receives a command line with no command on it (it replies B360;)" | Selector in the toolbar, and a bare Enter now genuinely sends the terminator. Source of that reply found at `custom_cmd.c:2307`. Defaults differ by transport: CR over USB, because `read_to_process()` terminates on `\r` *or* `\n` with no pair suppression, so a CRLF ends the line twice and draws a spurious extra reply. |
| **Save the data when the user types the waveform commands** rather than only when the Capture button is used | Typed rows are written with the same metadata plus the command that produced them. This exposed a silent bug: reply tokens share a line with the first CSV row, so matching whole lines dropped sample 0 with no warning. Rows are now matched anywhere in the line. |
| A **phase-align toggle** — "the starting point is based on when WG is triggered, so the same test repeated will show the waves out of phase when someone may want to just confirm their wave is still the same" | Checkbox that shifts each trace onto the first visible trace's first cycle. Measured on the two captures in-tree: 68.1% → 1.0% of Vpp overlay error. Display only; saved CSV keeps real time-since-GO. |
| **An embedded digital signature** so Windows does not flag it | Investigated, then **rejected on the evidence**: a self-signed certificate embeds a real signature but fails chain validation, so SmartScreen still warns and it looks worse than none. Concluded "forget about signing it that's ridiculous… unsigned exe + sha256 hash. That works for me." Shipped unsigned with a version resource, an icon and a SHA-256. Also established the app never requests elevation (`asInvoker`), so there is no UAC prompt at all. |
| Installing a certificate on customer machines is **unrealistic** | Agreed and dropped. The signing script was deleted rather than left dormant; the checksum moved into the build script. |
| **One light/dark toggle** on the terminal applying to both windows — the console was "somewhat mixed and the waveform is light mode" | Palettes became CSS custom properties, including the 14 colours the canvas had hardcoded, so there is one palette rather than two. The toggle lives only in the terminal and is pushed to the waveform window, including one opened after the toggle. |
| **Change-folder reachable from the main window**, "regardless of if the waveform window is ever opened" | Capture folder and auto-save in the terminal's bottom bar, kept in the waveform window too, synchronised both ways. |
| A **Quickstart / manual**, a **README as .txt**, and this plan saved with the source | `docs/B360_console_Quickstart_draftOrig.md`, `tools/B360_Console/README.txt`, `tools/B360_Console/b360_console_plan.md`. |
| **`b360_console.html` opened in a browser**, as an option for sites that will not run an outside `.exe` | Cannot be made to work — a `file://` page has no way to open a TCP or serial socket, and the page reaches the board only through the pywebview bridge. What it *was* doing was hanging, so it now detects the missing bridge and says so. The browser-only route is the **viewer** page with a pasted dump, which needs nothing installed. |
| **Options for sites that will not run an outside `.exe`** | Three, documented in the Quickstart: run the same program from source (`requirements.txt` pins pywebview 6.2.1 and pyserial 3.5; verified from a clean venv), `b360_capture.py` over TCP on a stdlib-only interpreter (verified in a `--without-pip` venv), or terminal plus the viewer page with nothing installed at all. Sharing the source was already the design -- the `.exe` bundles these same files rather than replacing them. **Chosen: the capture script.** It is the only one that asks for nothing at all, and the CSV it writes opens in the viewer page, which also asks for nothing. |
| **A folder of its own** -- the console files were "mixed with other test stuff that is unrelated" | Moved to `tools/B360_Console/`, `git mv` so the history follows. The folder is self-contained: nothing outside it is imported, and the build script was already `pushd "%~dp0"`-relative, so it needed no change. Ignore rules and doc paths repointed. |

## Rejected, and why — do not re-propose without new information

- **UDP for bulk transfer.** Offered as available, but `cmdETH2()` in
  `loopback_udps()` is commented out and the datalog sender below it is disabled:
  it receives a datagram and discards it. Would mean a firmware data path *and* an
  application-level retransmit layer, which is work TCP already does.
- **Capture over USB while a terminal holds TCP.** Looks like it sidesteps the
  single socket; actually corrupts transfers via `cmd_src`.
- **Self-signed code signing.** See above.
- **Full VT100 emulation.** The device sends no escape sequences.
- **Making `format_and_send()` retry on `SOCK_BUSY`.** The non-blocking,
  no-retry reply path is intentional -- blocking there would stall the command
  loop for every command, not just a bulk read. The host is the side that can
  afford to wait and ask again, so the windowed validation lives there.

## Bugs found only by running it

Three would not show up in review, and are recorded so they are not reintroduced:

- A page global named `history` is `window.history`, a getter-only property.
  Assigning to it under `'use strict'` throws and silently kills the rest of the
  script — the terminal was dead on arrival with no visible error.
- The CSV row pattern rejected a real dump, because the firmware glues the
  reply's `"; "` onto the *last* row instead of a line of its own.
- Connect in a browser hung with no error and no timeout. `api()` returned
  `null`, so the click set the status to *connecting…* and then threw a
  `TypeError` before a request existed — nothing to fail, nothing to time out,
  reload the only way out. A missing bridge now yields a stand-in that rejects,
  so the UI always unwinds.

## Status

The executable was verified against real hardware after the work above was
finished. Everything before that point was tested against a mock reproducing the
firmware's exact reply framing, and end-to-end through the real bridge in both
windows.

The bench checklist is in `docs/b360_console_draftOrig.md` if any of it needs re-running
after a change.
