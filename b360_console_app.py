#!/usr/bin/env python3
"""B360 console: a terminal and a waveform viewer sharing one board connection.

Ships as a single executable. The recipient installs nothing -- the bundle
carries its own interpreter, and the window is the WebView2 control that is
already part of Windows.

Why this is one application rather than a capture button beside PuTTY: the board
has exactly one TCP command socket, with no accept backlog, so while a terminal
holds it nothing else can connect. Capturing over USB instead is not a way out --
`cmd_src` is a single global set per command, so a keystroke in another terminal
part-way through a transfer reroutes the rest of the reply to the other
transport. Owning the link is the only arrangement in which neither can happen.

Two windows, one connection:

  * the terminal, which is the main window
  * a waveform window holding the plot, traces, statistics and capture controls

Closing the waveform window hides it rather than destroying it, so the traces
loaded in it survive being closed and reopened.

Usage:
    b360_console.exe [capture.csv ...]
    b360_console.exe --selftest            # assemble and exit, no window
    b360_console.exe --smoke 4             # open, then close after 4 seconds
"""

import base64
import json
import os
import re
import sys
import tempfile
import threading
import time
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import product                      #< after the path fix, so a source run finds it

#: First eight bytes of any PNG file, checked before writing one.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

CONSOLE_HTML = "b360_console.html"
VIEWER_HTML = "b360_wave_viewer.html"
MARKER = "<!--PRELOAD-->"
MAX_CSV_BYTES = 64 * 1024 * 1024


def out(msg, err=False):
    """Write a line, tolerating a windowed build where there is no console."""
    stream = sys.stderr if err else sys.stdout
    if stream is None:
        return
    try:
        stream.write(msg + "\n")
        stream.flush()
    except Exception:
        pass


def die(msg):
    """Fatal error. Uses a message box when there is no console to print to."""
    out(msg, err=True)
    if sys.stderr is None and sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, msg, product.P.title, 0x10)
        except Exception:
            pass
    raise SystemExit(2)


asset = product.asset           #< re-exported; product.py owns it (see its docstring)


def read_asset(name):
    """Read a bundled page with the product tokens filled in.

    The one choke point for loading a page, so {{PRODUCT}}/{{EXE}} cannot be
    missed. inject() splices preloaded CSV in afterwards, which is the right
    order: data must never be token-substituted.
    """
    path = asset(name)
    if not os.path.isfile(path):
        die("bundled page not found:\n%s" % path)
    with open(path, "r", encoding="utf-8") as fh:
        return product.P.fill(fh.read())


# --------------------------------------------------------------- settings ---

def settings_path():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or tempfile.gettempdir()
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "b360_console", "settings.json")


#: Settings keys that belong to one product rather than to the installation. An
#: address with no record of which product it belongs to is a hazard, so the whole
#: connection group lives in settings["products"][<name>] -- keeping the group
#: together means there is no judgement call about which key is safe to share, and
#: a product on a different port needs no code change.
#:
#: Everything else (theme, activity_log, autosave, save_dir, png_dir) is shared:
#: set the captures folder once, for every product.
PER_PRODUCT = ("kind", "host", "port", "eol")


def load_settings():
    try:
        with open(settings_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_settings(data):
    """Best effort. A read-only profile must not break the application."""
    try:
        path = settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except Exception:
        pass


def default_save_dir():
    home = os.path.expanduser("~")
    docs = os.path.join(home, "Documents")
    return os.path.join(docs if os.path.isdir(docs) else home,
                        product.P.captures_folder)


def default_png_dir():
    """Plot images live beside the captures they were drawn from."""
    return os.path.join(default_save_dir(), "saved_PNGs")


#: Characters Windows will not accept in a file name. The plot title becomes the
#: file name, and a title is free text, so this is not a theoretical case -- a
#: plot called "ch A/B 10 kHz" would otherwise be a write into a subdirectory.
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


#: Names Windows reserves for devices, which cannot be files whatever the suffix.
_RESERVED = {"con", "prn", "aux", "nul"}
_RESERVED |= {"com%d" % i for i in range(1, 10)}
_RESERVED |= {"lpt%d" % i for i in range(1, 10)}


def safe_name(name, fallback, suffix):
    """A plain file name: no directory part, no reserved characters, right suffix.

    The separators are replaced before basename() is asked for the last element,
    not after: a plot called "ch A/B 10 kHz" is one name with a slash in it, and
    taking the basename first would silently save it as "B 10 kHz".
    """
    name = _BAD_NAME.sub("_", str(name or "").strip())
    name = os.path.basename(name).strip(". ")   #< Windows drops both silently
    if not name or name.lower() in _RESERVED:
        name = fallback
    if not name.lower().endswith(suffix):
        name += suffix
    return name


def file_dialog_type(kind):
    """`FileDialog.SAVE` / `FileDialog.FOLDER`, however this pywebview spells it.

    pywebview 6.2.1 still answers to SAVE_DIALOG and FOLDER_DIALOG, but only as
    module properties that log "deprecated and will be removed in a future
    version" on every access (`webview/__init__.py:96-109`). The FileDialog enum
    holds the same values, so preferring it costs nothing and keeps the console
    working past that removal.
    """
    import webview
    names = getattr(webview, "FileDialog", None)
    if names is not None:
        return getattr(names, kind)
    return getattr(webview, kind + "_DIALOG")       #< pywebview older than 5


def trace_label(filename, saved):
    """The name a trace carries: a file name only when a file exists.

    The pages use this as the trace's label, and a label ending in .csv reads as
    "this is on disk". With auto-save off nothing was written, so the extension
    comes off -- the operator should not have to check the folder to find out.
    csvName() in the viewer puts .csv back if they later save it by hand.
    """
    if saved:
        return filename
    return filename[:-4] if filename.lower().endswith(".csv") else filename


def decode_data_url(text):
    """The bytes carried by a `data:` URL, which is how a canvas crosses the bridge."""
    head, sep, payload = str(text or "").partition(",")
    if not sep or not head.startswith("data:"):
        raise ValueError("expected a data: URL")
    if "base64" not in head:
        raise ValueError("expected a base64 data: URL")
    return base64.b64decode(payload, validate=False)


# ------------------------------------------------------------ csv plumbing ---

def read_csv_file(path):
    size = os.path.getsize(path)
    if size > MAX_CSV_BYTES:
        raise ValueError("%.1f MB is larger than this viewer will load"
                         % (size / 1048576.0))
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return os.path.basename(path), fh.read()


def inject(html, payload):
    """Splice preloaded captures into the viewer page at its marker.

    json.dumps does the escaping; the one thing it does not escape for HTML is a
    literal </script> inside the data, which would end the block early.
    """
    if not payload:
        return html
    blob = json.dumps(payload).replace("</", "<\\/")
    tag = "<script>window.B360_PRELOAD=%s;</script>" % blob
    if MARKER not in html:
        die("viewer page is missing its %s marker" % MARKER)
    return html.replace(MARKER, tag, 1)


def collect(paths):
    payload, problems = [], []
    for p in paths:
        try:
            name, text = read_csv_file(p)
            payload.append({"name": name, "text": text})
        except (OSError, ValueError) as exc:
            problems.append("%s: %s" % (p, exc))
    return payload, problems


def write_temp(html, tag):
    fd, path = tempfile.mkstemp(prefix="b360_%s_" % tag, suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path


def file_url(path):
    return "file:///" + os.path.abspath(path).replace(os.sep, "/").lstrip("/")


#: Silence after which the indicator stops claiming a verified link. Nothing is
#: sent to find out -- the board must never see a command nobody typed, because
#: the front panel indicates received commands. So the display says what is
#: actually known: the socket is open, and the board last answered N seconds ago.
STALE_S = 10.0

#: How often the "last reply N s ago" figure is pushed while it is showing. The
#: counter has to keep moving -- a frozen number reads as a frozen program.
STALE_REFRESH_S = 1.0

#: Reconnect backoff in seconds; the last value repeats for as long as it takes.
RETRY_BACKOFF = (1.0, 2.0, 4.0, 8.0)


# ------------------------------------------------------------- the bridge ---

#: Optional GUI windows beyond the terminal. A product's profile lists which ids
#: are live; everything here is gated on that, so one page and one Api serve every
#: product with no second copy to keep in sync.
#:
#: `writes` declares whether the panel SETS things on the device rather than
#: displaying the result of a query. A writing panel sends commands the operator
#: never typed, which the board's front panel indicates as activity -- so it must
#: echo every line it sends into the activity log, the way Api.capture()'s
#: echo()/flush() pair already does, and must refuse while app.capturing is set.
#: Nothing declares writes:True yet; the field is where that contract is recorded.
PANELS = {
    "waveform": {
        "page":   "b360_wave_viewer.html",
        "title":  "%s Waveform",          #< % the product name
        "label":  "the waveform viewer",  #< how a refusal names it
        "ui":     ("plotwin", "capbar", "wfhint"),   #< console elements it owns
        "writes": False,
    },
}


def panels_on():
    """Ids of the panels this product was built with."""
    return tuple(k for k in PANELS if product.P.has(k))


def panel(panel_id):
    """Gate an Api method on a panel being part of this product.

    Sits under @guard so a refusal still comes back in the standard envelope, and
    copies __name__/__doc__ for the same reason guard does.
    """
    def deco(fn):
        def wrapper(self, opts=None):
            if not product.P.has(panel_id):
                return {"ok": False,
                        "error": "%s is not part of %s"
                                 % (PANELS[panel_id]["label"], product.P.title)}
            return fn(self, opts)
        wrapper.__name__ = fn.__name__
        wrapper.__doc__ = fn.__doc__
        return wrapper
    return deco


def guard(fn):
    """Uniform {ok, error} envelope, so a raised exception never reaches JS."""
    def wrapper(self, opts=None):
        try:
            return fn(self, opts or {})
        except Exception as exc:
            from b360_link import LinkError
            closed = isinstance(exc, LinkError)
            if closed:
                self._app.board = None
                self._app.link_lost()
            return {"ok": False, "closed": closed,
                    "error": "%s" % exc if closed else
                             "%s: %s" % (exc.__class__.__name__, exc)}
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class Api:
    """Exposed to both pages as `window.pywebview.api`."""

    def __init__(self, app):
        self._app = app

    # -- connection ----------------------------------------------------------

    @guard
    def get_defaults(self, opts):
        from b360_link import DEFAULT_EOL_TCP, DEFAULT_EOL_SERIAL
        app = self._app
        return {"ok": True,
                "product": product.P.name,
                "panels": list(panels_on()),
                # the connection comes from this product's own section
                "kind": app.setting("kind", "tcp"),
                "host": app.setting("host", ""),
                "port": app.setting("port", 5007),
                "eol": app.setting("eol", ""),
                "eol_tcp": DEFAULT_EOL_TCP,
                "eol_serial": DEFAULT_EOL_SERIAL,
                "save_dir": app.setting("save_dir", default_save_dir()),
                "png_dir": app.setting("png_dir", default_png_dir()),
                "autosave": app.setting("autosave", True),
                "activity_log": app.setting("activity_log", True),
                "theme": app.setting("theme", "light")}

    @guard
    def list_serial(self, opts):
        from b360_link import list_serial_ports
        return list_serial_ports()

    @guard
    def connect(self, opts):
        app = self._app
        # Cleared before anything else: a Connect supersedes any reconnect in
        # progress, and if this attempt fails nothing may go on reconnecting to
        # the board we were on before -- the operator asked for a different one.
        app.link_want = False
        if app.board:
            app.board.close()
            app.board = None
        if opts.get("kind", "tcp") == "serial" and not opts.get("serial_port"):
            return {"ok": False, "error": "no serial port selected"}
        board = app.open_board(opts)
        app.board = board
        app.link_opts = dict(opts)
        app.link_want = True                  #< watchdog and reconnect are live
        app.note_activity()
        # `eol` is deliberately not stored here. get_defaults() feeding it back
        # makes the page treat it as deliberately chosen, which stops it from
        # following the transport -- and CRLF over USB draws a spurious extra
        # reply. Only set_eol(), which is the operator choosing, pins it.
        app.remember(kind=opts.get("kind", "tcp"), host=opts.get("host", ""),
                     port=int(opts.get("port") or 5007))
        app.board_id = app.identify()
        app.link_js("open", board.name)   #< the waveform window has no other cue
        return {"ok": True, "name": board.name, "id": app.board_id}

    @guard
    def disconnect(self, opts):
        app = self._app
        app.link_want = False                 #< stops any reconnect in progress
        if app.board:
            app.board.close()
            app.board = None
        app.link_js("closed")
        return {"ok": True}

    @guard
    def set_eol(self, opts):
        """Change what Enter sends, without dropping the connection."""
        from b360_link import eol_bytes
        name = opts.get("eol", "crlf")
        if self._app.board:
            self._app.board.eol = eol_bytes(name)
        self._app.remember(eol=name)
        return {"ok": True, "eol": name}

    @guard
    def send(self, opts):
        app = self._app
        if not app.board:
            # closed, so the page resets its indicator instead of sitting green
            # while every command comes back "not connected".
            return {"ok": False, "closed": True, "error": "not connected"}
        if app.capturing:
            return {"ok": False,
                    "error": "a waveform capture is in progress; wait for it to finish"}
        # An empty line is deliberately still sent: the firmware answers a line
        # with no command on it with its model name.
        line = opts.get("line", "")
        reply = app.board.command(line, timeout=float(opts.get("timeout") or 20.0))
        if line.strip().lower() == "id" and reply.strip():
            from b360_link import strip_frame
            flat = " ".join(strip_frame(reply).replace("\r", "\n").split())
            app.board_id = flat[:160]           #< the operator identified it
        # Only a real answer counts as activity. A silent board leaves the
        # timestamp stale, so the watchdog probes on its next tick instead of
        # waiting out another idle period.
        if reply.strip():
            app.note_activity()
        return {"ok": True, "reply": reply}

    # -- waveform ------------------------------------------------------------

    @guard
    @panel("waveform")
    def capture(self, opts):
        import b360_capture as cap
        app = self._app
        if not app.board:
            return {"ok": False, "error": "not connected"}
        if app.capturing:
            return {"ok": False, "error": "a capture is already running"}

        notes = []

        def progress(msg):
            # Every progress message follows a reply the board actually sent, so
            # a long capture keeps the link fresh instead of ageing into "stale".
            app.note_activity()
            notes.append(msg)
            app.plot_js("b360_capture_progress", msg)
            app.console_js("b360_event", "note", "capture: " + msg)

        # Every command the capture sends, shown in the terminal. `WS` is polled
        # to DONE, so consecutive repeats are counted rather than printed once
        # each -- the count is the honest form, a single line would not be.
        run = {"line": "", "n": 0}

        def flush():
            if run["n"]:
                app.console_js("b360_event", "note", "> " + run["line"]
                               + (" x%d" % run["n"] if run["n"] > 1 else ""))
            run["line"], run["n"] = "", 0

        def echo(line):
            if line == run["line"]:
                run["n"] += 1
                return
            flush()
            run["line"], run["n"] = line, 1

        app.capturing = True
        try:
            rate = opts.get("rate") or None
            channel = opts.get("channel")
            channel = None if channel in ("", None) else channel
            text = cap.capture(app.board,
                               rate_hz=rate,
                               channel=channel,
                               start=int(opts.get("start") or 0),
                               count=int(opts.get("count") or cap.WAVE_DEPTH),
                               window=int(opts.get("window") or cap.DEFAULT_WINDOW),
                               retries=int(opts.get("retries") or 2),
                               progress=progress, echo=echo,
                               product=product.P.name)
        finally:
            flush()
            app.capturing = False

        if app.board_id:
            banner = "# %s waveform capture\n" % product.P.name
            text = text.replace(banner, banner + "# board: %s\n" % app.board_id, 1)

        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        fname = "%s%s.csv" % (product.P.capture_prefix, stamp)
        saved = app.autosave(fname, text)
        app.console_js("b360_event", "note",
                       "capture complete: %s" % (saved or "not saved to disk"))
        return {"ok": True, "csv": text, "name": trace_label(fname, saved),
                "saved": saved,
                "retried": any("retried" in n for n in notes)}

    @guard
    @panel("waveform")
    def save_capture(self, opts):
        """Turn rows typed out by hand into a proper capture file.

        The terminal hands over rows it recognised in a reply; this adds the
        header and the same metadata the Capture button writes, so a capture taken
        by typing `wc`/`wg`/`ws`/`wr` is indistinguishable afterwards from one
        taken with the button.
        """
        import b360_capture as cap
        app = self._app
        rows = opts.get("rows") or []
        if len(rows) < 2:
            return {"ok": False, "error": "no waveform rows in that reply"}

        times = []
        for r in rows:
            try:
                times.append(float(str(r).split(",")[0]))
            except (ValueError, IndexError):
                pass
        # Rate comes from the data rather than from asking the board: the time
        # column is generated from the divisor latched at WG, so this is exact and
        # costs no extra command in the middle of the user's session.
        rate = 0.0
        if len(times) > 1:
            steps = sorted(times[i] - times[i - 1] for i in range(1, len(times)))
            step = steps[len(steps) // 2]
            if step > 0:
                rate = 1.0 / step

        head = ["# %s waveform capture" % product.P.name]
        if app.board_id:
            head.append("# board: %s" % app.board_id)
        if app.board:
            head.append("# source: %s" % app.board.name)
        head.append("# command: %s" % str(opts.get("command", "")).strip())
        if rate:
            head.append("# sample_rate_hz: %g (derived from the time column)" % rate)
        head.append("# samples: %d" % len(rows))
        head.append("# captured_utc: %s"
                    % datetime.datetime.now(datetime.timezone.utc)
                    .strftime("%Y-%m-%dT%H:%M:%SZ"))
        head.append("# voltage is at the ADC pin; the 1k/4.53k divider is not undone")
        head.append(cap.CSV_HEADER)

        text = "\n".join(head + [str(r) for r in rows]) + "\n"
        fname = "%s%s.csv" % (product.P.capture_prefix,
                              datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        saved = app.autosave(fname, text)
        return {"ok": True, "csv": text, "name": trace_label(fname, saved),
                "saved": saved, "samples": len(rows)}

    @guard
    @panel("waveform")
    def plot_text(self, opts):
        """Hand a block of CSV from the terminal to the waveform window."""
        self._app.show_plot_window()
        self._app.plot_js("b360_receive", opts.get("name", "console"),
                          opts.get("text", ""))
        return {"ok": True}

    @guard
    @panel("waveform")
    def show_plot(self, opts):
        self._app.show_plot_window()
        return {"ok": True}

    @guard
    @panel("waveform")
    def save_csv(self, opts):
        """Write CSV where the user asks. Falls back to the autosave folder."""
        app = self._app
        name = opts.get("name") or (product.P.capture_prefix + ".csv")
        text = opts.get("text") or ""
        path = None
        if app.plot is not None:
            try:
                chosen = app.plot.create_file_dialog(
                    file_dialog_type("SAVE"), save_filename=name,
                    file_types=("CSV (*.csv)", "All files (*.*)"))
                if not chosen:
                    return {"ok": False, "error": "cancelled"}
                path = chosen if isinstance(chosen, str) else chosen[0]
            except Exception:
                path = None
        if path is None:
            path = app.autosave(name, text)
            return {"ok": bool(path), "path": path,
                    "error": None if path else "could not write the file"}
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return {"ok": True, "path": path}

    @guard
    @panel("waveform")
    def save_png(self, opts):
        """Write the plot image, and remember where the operator put it.

        This exists because the page cannot save one by itself. Handing a blob to
        an <a download> is a browser download, and pywebview cancels those
        outright unless settings['ALLOW_DOWNLOADS'] is set -- its WebView2
        DownloadStarting handler sets args.Cancel and returns
        (`webview/platforms/edgechromium.py:316-319`), and the default is False
        (`webview/__init__.py:120`). This application never sets it, so the old
        Save PNG button did nothing at all inside the executable: no file, no
        dialog, no error. The CSV path already goes through the host (save_csv).

        The folder offered is whichever one was used last, starting at
        default_png_dir(). Saving elsewhere moves that default, so the dialog
        opens where the operator was working rather than where the program
        thought they should be.
        """
        app = self._app
        name = safe_name(opts.get("name"), product.P.png_fallback, ".png")
        raw = decode_data_url(opts.get("data"))
        if not raw.startswith(PNG_MAGIC):
            return {"ok": False, "error": "that is not a PNG image"}

        folder = app.settings.get("png_dir") or default_png_dir()
        try:
            os.makedirs(folder, exist_ok=True)
        except OSError:
            folder = ""

        path = None
        # Prefer the window that holds the plot; fall back to the terminal so a
        # save still works if the waveform window is in an odd state.
        win = app.plot or app.console
        if win is not None:
            try:
                chosen = win.create_file_dialog(
                    file_dialog_type("SAVE"), directory=folder, save_filename=name,
                    file_types=("PNG image (*.png)", "All files (*.*)"))
                if not chosen:
                    return {"ok": False, "error": "cancelled"}
                path = chosen if isinstance(chosen, str) else chosen[0]
            except Exception:
                path = None                 #< no dialog available; write it below

        if path is None:
            if not folder:
                return {"ok": False, "error": "could not create %s" % default_png_dir()}
            path = os.path.join(folder, name)
        # A dialog whose filter was switched to "All files" hands back whatever
        # was typed, which may have no extension at all.
        if not path.lower().endswith(".png"):
            path += ".png"

        with open(path, "wb") as fh:
            fh.write(raw)
        app.remember(png_dir=os.path.dirname(os.path.abspath(path)))
        return {"ok": True, "path": path}

    @guard
    @panel("waveform")
    def choose_save_dir(self, opts):
        app = self._app
        # Prefer the window that asked; the terminal must work on its own, since
        # the waveform window may never have been opened.
        win = app.console or app.plot
        chosen = win.create_file_dialog(file_dialog_type("FOLDER"))
        if not chosen:
            return {"ok": False, "error": "cancelled"}
        path = chosen if isinstance(chosen, str) else chosen[0]
        app.remember(save_dir=path)
        app.announce_settings()
        return {"ok": True, "save_dir": path}

    @guard
    def set_theme(self, opts):
        """One toggle for both windows, so they cannot drift apart."""
        mode = 'dark' if opts.get("theme") == 'dark' else 'light'
        app = self._app
        app.remember(theme=mode)
        app.broadcast("b360_set_theme", mode)
        return {"ok": True, "theme": mode}

    @guard
    def set_activity_log(self, opts):
        """Remember whether the terminal shows the app's own grey lines."""
        app = self._app
        app.remember(activity_log=bool(opts.get("on", True)))
        return {"ok": True}

    @guard
    @panel("waveform")
    def set_autosave(self, opts):
        app = self._app
        app.remember(autosave=bool(opts.get("on", True)))
        app.announce_settings()
        return {"ok": True}


# ------------------------------------------------------------- application ---

class App:
    def __init__(self, preload):
        self.settings = load_settings()
        self.board = None
        self.board_id = ""
        self.capturing = False
        self.link_opts = {}            #< what connect() used, replayed to reconnect
        self.link_want = False         #< the user wants a link; clears on Disconnect
        self.last_ok = 0.0             #< monotonic time of the last real reply
        self._relink = threading.Lock()
        self._call_errs = set()        #< push failures already reported
        self._watchdog = None
        self.console = None
        self.plot = None
        self.plot_url = None
        self.preload = preload
        self.shutting_down = False
        self.api = Api(self)

    # -- settings -----------------------------------------------------------

    def setting(self, key, default=None):
        """Read a setting, from this product's section for a per-product key."""
        if key in PER_PRODUCT:
            return (self.settings.get("products", {})
                                 .get(product.P.name, {})
                                 .get(key, default))
        return self.settings.get(key, default)

    def remember(self, **kw):
        """Persist settings, routing per-product keys into this product's section.

        Re-reads the file and changes only these keys. Two consoles can be open at
        once, and writing a whole dict back from a stale copy would erase the other
        product's section. It also leaves a pre-sectioning file's top-level keys
        alone: they have no provable owner, so nothing claims them.
        """
        data = load_settings()
        for key, value in kw.items():
            if key in PER_PRODUCT:
                data.setdefault("products", {}).setdefault(product.P.name, {})[key] = value
            else:
                data[key] = value
        save_settings(data)
        self.settings = data

    # -- pushing into the pages ---------------------------------------------

    def _call(self, win, fn, *args):
        """Invoke a JS function in `win`. ensure_ascii keeps U+2028/9 out."""
        if win is None:
            return
        try:
            win.evaluate_js("%s(%s)" % (fn, ",".join(json.dumps(a) for a in args)))
        except Exception as exc:
            # This is the only channel to the pages, so a failure here is exactly
            # how an indicator ends up frozen on a state that is no longer true.
            # Logged once per distinct cause: the watchdog pushes twice a second
            # and a persistent failure would otherwise flood the log.
            key = "%s/%s" % (fn, exc.__class__.__name__)
            if key not in self._call_errs:
                self._call_errs.add(key)
                out("could not update the UI (%s): %s: %s"
                    % (fn, exc.__class__.__name__, exc), err=True)

    def console_js(self, fn, *args):
        self._call(self.console, fn, *args)

    def plot_js(self, fn, *args):
        self._call(self.plot, fn, *args)

    def broadcast(self, fn, *args):
        """Call the same JS function in both windows."""
        self._call(self.console, fn, *args)
        self._call(self.plot, fn, *args)

    def announce_settings(self):
        """Push the capture destination to both windows so they agree."""
        self.broadcast("b360_settings_changed",
                       self.settings.get("save_dir") or default_save_dir(),
                       bool(self.settings.get("autosave", True)))

    # -- behaviour -----------------------------------------------------------

    def open_board(self, opts):
        """Open the link described by `opts`. Raises LinkError if it cannot."""
        from b360_link import Board, DEFAULT_EOL_TCP, DEFAULT_EOL_SERIAL
        kind = opts.get("kind", "tcp")
        eol = opts.get("eol") or (DEFAULT_EOL_SERIAL if kind == "serial"
                                  else DEFAULT_EOL_TCP)
        if kind == "serial":
            return Board.serial(opts.get("serial_port") or "",
                                int(opts.get("baud") or 115200), eol=eol)
        return Board.tcp(opts.get("host", ""), int(opts.get("port") or 5007),
                         eol=eol)

    # -- link watchdog -------------------------------------------------------

    def note_activity(self):
        """Record that the board actually answered something."""
        self.last_ok = time.monotonic()

    def link_js(self, state, text=""):
        self.broadcast("b360_link_state", state, text)

    def start_watchdog(self):
        if self._watchdog is None:
            self._watchdog = threading.Thread(target=self._watch, daemon=True)
            self._watchdog.start()

    def _watch(self):
        """Report what is known about the link without ever writing to it.

        Three passive sources, none of which the board can see: the socket
        reporting a FIN or RST, the USB port disappearing from the OS, and how
        long it has been since the board last answered something the operator
        asked for. A quiet board on a healthy link is indistinguishable from an
        unplugged one without sending it something, so the display says "not
        verified" rather than guessing either way.
        """
        stale, pushed = False, 0.0
        while not self.shutting_down:
            time.sleep(0.5)
            board = self.board
            if board is None or not self.link_want or self.capturing:
                stale = False
                continue
            if board.transport.peer_gone():
                stale = False
                self.link_lost()
                continue
            quiet = time.monotonic() - self.last_ok
            if quiet >= STALE_S:
                now = time.monotonic()
                if not stale or (now - pushed) >= STALE_REFRESH_S:
                    stale, pushed = True, now
                    self.link_js("stale", "%d" % quiet)
            elif stale:
                stale = False
                # "fresh", not "up": the link never dropped, so this only clears
                # the counter. 'up' is a reconnect and says so in the transcript.
                self.link_js("fresh", board.name)

    def link_lost(self):
        """Announce the dead link and reconnect in the background."""
        if not self.link_want or self.shutting_down:
            return
        if not self._relink.acquire(blocking=False):
            return                      #< a reconnect is already running
        threading.Thread(target=self._relink_loop, daemon=True).start()

    def _relink_loop(self):
        try:
            board, self.board = self.board, None
            # Nothing is sent on reconnect, so the identity goes unconfirmed.
            # The capture header omits the board rather than carrying a line that
            # was true before the drop; `ID` in the terminal restores it.
            self.board_id = ""
            if board:
                try:
                    board.close()
                except Exception:
                    pass
            self.link_js("lost", "link lost")
            attempt = 0
            while self.link_want and not self.shutting_down:
                delay = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                waited = 0.0
                while waited < delay:
                    if not self.link_want or self.shutting_down:
                        return
                    time.sleep(0.25)
                    waited += 0.25
                if not self.link_want or self.shutting_down:
                    return              #< Disconnect landed during the backoff
                attempt += 1
                self.link_js("retry", "reconnecting, attempt %d" % attempt)
                try:
                    board = self.open_board(self.link_opts)
                except Exception:
                    continue
                if not self.link_want or self.board is not None:
                    board.close()       #< Disconnect, or a manual Connect, won
                    return
                self.board = board
                self.note_activity()
                self.link_js("up", board.name)
                return
        finally:
            self._relink.release()

    def identify(self):
        """One-line board identity for capture metadata. Never fatal."""
        try:
            from b360_link import strip_frame
            raw = strip_frame(self.board.command("ID", timeout=5.0))
            flat = " ".join(raw.replace("\n\r", "\n").replace("\r", "\n").split())
            if flat:
                self.note_activity()
            return flat[:160]
        except Exception:
            return ""

    def autosave(self, name, text):
        """Drop a copy in the capture folder. Returns the path, or ''."""
        if not self.settings.get("autosave", True):
            return ""
        folder = self.settings.get("save_dir") or default_save_dir()
        try:
            os.makedirs(folder, exist_ok=True)
            path = os.path.join(folder, name)
            with open(path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            return path
        except OSError as exc:
            self.console_js("b360_event", "err", "could not autosave: %s" % exc)
            return ""

    def show_plot_window(self):
        # One guard covers every caller -- a CSV on the command line, --smoke and
        # the Api -- so a build without the panel cannot try to open a page that
        # is not in its bundle.
        if not product.P.has("waveform"):
            return False
        if self.plot is None:
            self._create_plot_window()
        else:
            try:
                self.plot.show()
            except Exception:
                self.plot = None
                self._create_plot_window()

    def _create_plot_window(self):
        import webview
        html = inject(read_asset(PANELS["waveform"]["page"]), self.preload)
        self.plot_url = write_temp(html, "wave")
        self.plot = webview.create_window(
            PANELS["waveform"]["title"] % product.P.name,
            url=file_url(self.plot_url),
            js_api=self.api, width=1180, height=780, min_size=(760, 520),
            text_select=True)
        # Closing hides rather than destroys, so loaded traces survive.
        self.plot.events.closing += self._plot_closing
        # The page reads the theme itself via get_defaults(), but a window created
        # after a toggle would otherwise miss it.
        self.plot.events.loaded += self._plot_loaded

    def _plot_loaded(self):
        self.plot_js("b360_set_theme", self.settings.get("theme", "light"))
        self.announce_settings()
        # Opened after connecting, this window would otherwise still be saying
        # "connect in the console window".
        if self.board is not None:
            self.plot_js("b360_link_state", "open", self.board.name)

    def _plot_closing(self):
        """Hide instead of closing, so loaded traces survive.

        Except during shutdown: refusing then would keep the application alive
        after the terminal window is gone.
        """
        if self.shutting_down:
            return True
        try:
            self.plot.hide()
        except Exception:
            pass
        return False

    def cleanup(self):
        self.link_want = False
        if self.board:
            self.board.close()
        for path in (self.plot_url,):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass


# -------------------------------------------------------------- entry point ---

def parse_flags(argv):
    """Split diagnostics off the argument list. Everything else is a CSV path."""
    files, selftest, smoke, which = [], False, 0.0, None
    it = iter(argv)
    for a in it:
        if a == "--selftest":
            selftest = True
        elif a == "--smoke":
            smoke = float(next(it, "4"))
        elif a == "--product":
            which = next(it, "")       #< source runs; a build bakes product.json
        elif a.startswith("--product="):
            which = a.split("=", 1)[1]
        else:
            files.append(a)
    return files, selftest, smoke, which


def run_selftest(payload, problems):
    """Report what this build is, and verdict on whether it can run.

    Rows are (label, value, ok) so the verdict comes from the checks themselves
    rather than from re-parsing the printed text -- the old version decided by
    looking for "no (" in lines starting "b360_", which would have failed every
    terminal-only build, since b360_capture is deliberately not bundled for one.
    """
    def importable(name):
        try:
            __import__(name)
            return "yes", True
        except ImportError as exc:
            return "no (%s)" % exc, False

    def page_check(name):
        """Loaded, token-substituted, and the preload marker still intact."""
        try:
            text = read_asset(name)
        except SystemExit:
            return "MISSING", False
        left = product.P.leftover_tokens(text)
        if left:
            return "unsubstituted %s" % " ".join(left), False
        return ("%d bytes, marker %s"
                % (len(text.encode("utf-8")),
                   "present" if MARKER in text else "absent")), True

    on = panels_on()
    rows = [("product", product.P.name, True),
            ("panels", ", ".join(on) or "none (terminal only)", True),
            ("console page", asset(CONSOLE_HTML), True),
            ("console html", ) + page_check(CONSOLE_HTML)]

    # Only a build that HAS the viewer is asked about the viewer. Reading it
    # unconditionally is what used to abort the selftest, and with it the build.
    if "waveform" in on:
        rows.append(("viewer page", asset(PANELS["waveform"]["page"]), True))
        rows.append(("viewer html", ) + page_check(PANELS["waveform"]["page"]))
        rows.append(("b360_capture", ) + importable("b360_capture"))

    rows += [
        ("preloaded", "%d file(s)%s"
            % (len(payload), ", %d skipped" % len(problems) if problems else ""),
         not problems),
        ("b360_link", ) + importable("b360_link"),
        ("pywebview", ) + importable("webview"),
        ("pyserial", ) + importable("serial"),
        ("settings file", settings_path(), True),
        ("capture folder", load_settings().get("save_dir") or default_save_dir(), True),
        ("PNG folder", load_settings().get("png_dir") or default_png_dir(), True),
        ("frozen", "%s" % bool(getattr(sys, "frozen", False)), True),
    ]

    report = ["%-14s: %s%s" % (label, value, "" if ok else "   <-- FAIL")
              for label, value, ok in rows]
    for line in report:
        out(line)
    for p in problems:
        out("note          : %s" % p)
    # A windowed build has no console, so leave the report somewhere readable.
    try:
        with open(os.path.join(tempfile.gettempdir(), product.P.selftest_log),
                  "w", encoding="utf-8") as fh:
            fh.write("\n".join(report) + "\n")
    except OSError:
        pass
    return 0 if all(ok for _, _, ok in rows) else 1


def main(argv):
    args, selftest, smoke, which = parse_flags(argv[1:])
    if which:
        try:
            product.use(which)
        except (ValueError, OSError) as exc:
            die("%s" % exc)
    payload, problems = collect(args)
    # A terminal-only build has nothing to plot, so say so rather than opening a
    # window that does not exist in its bundle.
    if payload and not product.P.has("waveform"):
        problems.append("%s has no waveform viewer, so %d CSV file(s) were ignored"
                        % (product.P.title, len(payload)))
        payload = []
    for p in problems:
        out("skipped %s" % p, err=True)

    if selftest:
        return run_selftest(payload, problems)

    try:
        import webview
    except ImportError:
        die("this build is missing pywebview, so it cannot open its window.\n"
            "The waveform viewer page can still be opened directly in a browser.")

    app = App(payload)
    console_url = write_temp(read_asset(CONSOLE_HTML), "console")
    # text_select defaults to False, on which pywebview injects
    # "body { user-select: none; cursor: default; }" (webview/js/customize.js:4-5)
    # and no transcript can be copied out. The pages narrow it back down to the
    # parts worth copying; see the selection rules in b360_console.html.
    app.console = webview.create_window(
        product.P.title, url=file_url(console_url), js_api=app.api,
        width=1040, height=720, min_size=(680, 440), text_select=True)

    def console_closing():
        """Closing the terminal ends the session.

        The waveform window normally refuses to close so its traces survive, so
        it has to be told that this time is different -- otherwise it would keep
        the application alive after the terminal is gone.
        """
        app.shutting_down = True
        app.link_want = False
        if app.plot is not None:
            try:
                app.plot.destroy()
            except Exception:
                pass
        return True

    app.console.events.closing += console_closing

    def on_start():
        app.start_watchdog()
        if payload:
            app.show_plot_window()
        if smoke:
            import time

            def shut():
                time.sleep(smoke)
                try:
                    app.show_plot_window()
                    time.sleep(1.0)
                except Exception:
                    pass
                try:
                    console_closing()
                    app.console.destroy()
                except Exception:
                    pass

            threading.Thread(target=shut, daemon=True).start()

    try:
        webview.start(on_start)
        out("window closed (backend: %s)" % getattr(webview, "guilib", "?"))
    finally:
        app.cleanup()
        try:
            os.unlink(console_url)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
