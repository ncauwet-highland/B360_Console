"""Which product this console is built as, and every string derived from it.

One source tree serves several products. The build writes a profile into the
bundle (`product.json`); everything the operator can see is derived from its
`name`, so two products cannot drift apart in the places nobody looks.

Deliberately stdlib-only and importable on its own: b360_capture.py is also a
standalone CLI and must not gain a dependency on the application. That is why
`asset()` lives here rather than in b360_console_app.py.

Identifiers are NOT derived. Module names, page filenames and the JS bridge names
(b360_event, b360_link_state, window.B360_PRELOAD) are a Python<->HTML contract,
not branding, and stay as they are whatever the product.
"""

import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

#: Built as this when nothing says otherwise -- running from source, mostly.
DEFAULT = "B360"

#: Shown in brackets after every window's title bar text. The title bar is set
#: by create_window(), not by the page's <title> -- pywebview never reads that.
VENDOR = "Highland Technology"

#: Profile written into the bundle by tools/gen_build_files.py.
BUNDLED = "product.json"

#: Tokens substituted into the pages and README. Not `{{` on its own: a page is
#: allowed to contain braces, so only these exact spellings are replaced.
TOK_PRODUCT = "{{PRODUCT}}"
TOK_EXE = "{{EXE}}"
TOK_BINARY = "{{BINARY}}"       #< the file the operator runs, e.g. B360TachView.exe
TOK_TITLE = "{{TITLE}}"         #< the application's name, e.g. B360TachView, B960 Console
TOK_BRAND = "{{BRAND}}"         #< the same without " Console", e.g. B360TachView, B960
TOKENS = (TOK_PRODUCT, TOK_EXE, TOK_BINARY, TOK_TITLE, TOK_BRAND)

#: What an "app" name may contain. It becomes the exe's file name and is passed
#: unquoted through build_console_exe.bat, so no spaces or path characters.
APP_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")

#: How the operator picks the window's look. "lightdark" is the Light/Dark select;
#: "wheel" is a colour wheel from which the page derives a whole palette, holding
#: every text and border colour to a minimum contrast against the picked colour.
APPEARANCES = ("lightdark", "wheel")


def asset(name):
    """Locate a bundled file: the PyInstaller payload dir, else alongside us."""
    base = getattr(sys, "_MEIPASS", None) or HERE
    return os.path.join(base, name)


class Product:
    """A product name plus the strings derived from it."""

    def __init__(self, name, blurb="", panels=(), appearance="", app=""):
        self.name = str(name).strip() or DEFAULT
        # Optional application name, replacing "<name> Console" and the
        # <slug>_console exe, and naming everything the waveform window shows or
        # writes: its title, the captures folder, capture files, their "# <app>
        # waveform capture" header and saved plots. The device is still `name`:
        # the .local host hint and the per-product settings section use that.
        self.app = str(app or "").strip()
        if self.app and not APP_NAME.match(self.app):
            raise ValueError("%s: app name %r must be letters, digits, '_', '.' "
                             "or '-' (it becomes the exe's file name)"
                             % (self.name, self.app))
        self.blurb = blurb or "terminal"
        self.panels = tuple(panels or ())
        self.appearance = appearance or APPEARANCES[0]
        if self.appearance not in APPEARANCES:
            raise ValueError("unknown appearance %r for %s (known: %s)"
                             % (appearance, self.name, ", ".join(APPEARANCES)))
        # The wheel derives the terminal's palette only. The waveform window has
        # its own (including 14 canvas colours) that nothing derives yet, so the
        # pair would give a custom terminal beside a stock Light/Dark plot.
        if self.appearance == "wheel" and "waveform" in self.panels:
            raise ValueError("%s: appearance \"wheel\" does not cover the "
                             "waveform panel yet" % self.name)

    # -- derived names -------------------------------------------------------

    @property
    def slug(self):
        return self.name.lower()

    @property
    def exe(self):
        """Executable and internal name: the app name as given, e.g.
        B360TachView, else <slug>_console, e.g. b960_console."""
        return self.app or "%s_console" % self.slug

    @property
    def binary(self):
        """The file the operator actually runs on this platform."""
        return self.exe + (".exe" if sys.platform == "win32" else ".AppImage")

    @property
    def title(self):
        """The application's name, e.g. B360TachView or B960 Console."""
        return self.app or "%s Console" % self.name

    @property
    def brand(self):
        """The title without a generic " Console": B360TachView, or B960."""
        return self.app or self.name

    @property
    def window_title(self):
        """Title bar text of the terminal window."""
        return "%s [%s]" % (self.title, VENDOR)

    @property
    def waveform_title(self):
        """Title bar text of the waveform window."""
        return "%s Waveform [%s]" % (self.brand, VENDOR)

    @property
    def captures_folder(self):
        """Leaf name under Documents, e.g. "B360TachView captures"."""
        return "%s captures" % self.brand

    @property
    def selftest_log(self):
        return "%s_selftest.txt" % self.exe

    @property
    def capture_prefix(self):
        """Capture file stem, e.g. B360TachView_capture_<stamp>.csv, or
        b960_capture_<stamp>.csv without an app name."""
        return "%s_capture_" % (self.app or self.slug)

    @property
    def png_fallback(self):
        """Name for a saved plot when the title box is empty."""
        return "%s_waveform" % self.brand

    # -- panels --------------------------------------------------------------

    def has(self, panel):
        return panel in self.panels

    # -- text substitution ---------------------------------------------------

    def fill(self, text):
        """Replace the product tokens. Used for both the pages and the README,
        so documentation and UI cannot disagree about the name."""
        return (text.replace(TOK_PRODUCT, self.name)
                    .replace(TOK_BINARY, self.binary)
                    .replace(TOK_EXE, self.exe)
                    .replace(TOK_TITLE, self.title)
                    .replace(TOK_BRAND, self.brand))

    def leftover_tokens(self, text):
        """Which tokens survived a fill(). Empty tuple is what we want."""
        return tuple(t for t in TOKENS if t in text)

    def __repr__(self):
        return ("Product(%r, app=%r, panels=%r, appearance=%r)"
                % (self.name, self.app, self.panels, self.appearance))


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not data.get("name"):
        raise ValueError("%s has no \"name\"" % path)
    return Product(data["name"], data.get("blurb", ""), data.get("panels", ()),
                   data.get("appearance", ""), data.get("app", ""))


def profile_path(name):
    return os.path.join(HERE, "products", "%s.json" % str(name).lower())


def load(name=None):
    """Resolve the active product.

    `name` wins (that is `--product` on the command line) and must exist, so a
    typo fails loudly instead of silently building the default. Otherwise the
    bundled profile, then the default profile from the tree, then a hardcoded
    fallback so an incomplete checkout still runs.
    """
    if name:
        path = profile_path(name)
        if not os.path.isfile(path):
            raise ValueError("no product profile for %r (looked for %s)"
                             % (name, path))
        return _read(path)

    bundled = asset(BUNDLED)
    if os.path.isfile(bundled):
        return _read(bundled)

    path = profile_path(DEFAULT)
    if os.path.isfile(path):
        return _read(path)

    return Product(DEFAULT, "terminal and waveform viewer", ("waveform",),
                   app="B360TachView")


#: The active product. main() replaces this when --product is given; everything
#: else reads it. Resolved at import so the module is usable on its own.
P = load()


def use(name):
    """Switch the active product. Only main() calls this, before any window."""
    global P
    P = load(name)
    return P
