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
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

#: Built as this when nothing says otherwise -- running from source, mostly.
DEFAULT = "B360"

#: Profile written into the bundle by tools/gen_build_files.py.
BUNDLED = "product.json"

#: Tokens substituted into the pages and README. Not `{{` on its own: a page is
#: allowed to contain braces, so only these exact spellings are replaced.
TOK_PRODUCT = "{{PRODUCT}}"
TOK_EXE = "{{EXE}}"
TOK_BINARY = "{{BINARY}}"       #< the file the operator runs, e.g. b360_console.exe


def asset(name):
    """Locate a bundled file: the PyInstaller payload dir, else alongside us."""
    base = getattr(sys, "_MEIPASS", None) or HERE
    return os.path.join(base, name)


class Product:
    """A product name plus the strings derived from it."""

    def __init__(self, name, blurb="", panels=()):
        self.name = str(name).strip() or DEFAULT
        self.blurb = blurb or "terminal"
        self.panels = tuple(panels or ())

    # -- derived names -------------------------------------------------------

    @property
    def slug(self):
        return self.name.lower()

    @property
    def exe(self):
        """Executable and internal name, e.g. b360_console."""
        return "%s_console" % self.slug

    @property
    def binary(self):
        """The file the operator actually runs on this platform."""
        return self.exe + (".exe" if sys.platform == "win32" else ".AppImage")

    @property
    def title(self):
        return "%s Console" % self.name

    @property
    def waveform_title(self):
        return "%s Waveform" % self.name

    @property
    def captures_folder(self):
        """Leaf name under Documents, e.g. "B360 captures"."""
        return "%s captures" % self.name

    @property
    def selftest_log(self):
        return "%s_selftest.txt" % self.exe

    @property
    def capture_prefix(self):
        """Capture file stem, e.g. b360_capture_ -> b360_capture_<stamp>.csv."""
        return "%s_capture_" % self.slug

    @property
    def png_fallback(self):
        """Name for a saved plot when the title box is empty."""
        return "%s_waveform" % self.name

    # -- panels --------------------------------------------------------------

    def has(self, panel):
        return panel in self.panels

    # -- text substitution ---------------------------------------------------

    def fill(self, text):
        """Replace the product tokens. Used for both the pages and the README,
        so documentation and UI cannot disagree about the name."""
        return (text.replace(TOK_PRODUCT, self.name)
                    .replace(TOK_BINARY, self.binary)
                    .replace(TOK_EXE, self.exe))

    def leftover_tokens(self, text):
        """Which tokens survived a fill(). Empty tuple is what we want."""
        return tuple(t for t in (TOK_PRODUCT, TOK_EXE, TOK_BINARY)
                     if t in text)

    def __repr__(self):
        return "Product(%r, panels=%r)" % (self.name, self.panels)


def _read(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not data.get("name"):
        raise ValueError("%s has no \"name\"" % path)
    return Product(data["name"], data.get("blurb", ""), data.get("panels", ()))


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

    return Product(DEFAULT, "terminal and waveform viewer", ("waveform",))


#: The active product. main() replaces this when --product is given; everything
#: else reads it. Resolved at import so the module is usable on its own.
P = load()


def use(name):
    """Switch the active product. Only main() calls this, before any window."""
    global P
    P = load(name)
    return P
