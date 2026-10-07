#!/usr/bin/env python3
"""Generate the per-product inputs PyInstaller needs, for build_console_exe.bat.

    python tools/gen_build_files.py B960

Writes into gen/:

    product.json            the profile, bundled so the exe knows what it is
    <exe>.version.txt       the Windows version resource, from version.txt.in
    build.env               KEY=VALUE lines the .bat reads back

Not into build/: that is PyInstaller's default workpath and --clean deletes it,
which would remove these inputs before Analysis could read them.

Python rather than batch string munging: cmd cannot substitute into a multi-line
template without quoting pain, and an unknown product name has to fail loudly
rather than quietly produce an exe built as the default.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import product                                      # noqa: E402

GEN = os.path.join(ROOT, "gen")
TEMPLATE = os.path.join(ROOT, "version.txt.in")


def main(argv):
    name = argv[1] if len(argv) > 1 and argv[1].strip() else product.DEFAULT
    try:
        p = product.load(name)
    except (ValueError, OSError) as exc:
        sys.stderr.write("gen_build_files: %s\n" % exc)
        sys.stderr.write("gen_build_files: profiles available: %s\n"
                         % ", ".join(sorted(
                             os.path.splitext(f)[0].upper()
                             for f in os.listdir(os.path.join(ROOT, "products"))
                             if f.endswith(".json") and not f.startswith("TEMPLATE"))))
        return 2

    os.makedirs(GEN, exist_ok=True)

    # The profile the exe reads back at runtime. Written from the loaded object,
    # not copied, so a malformed profile fails here rather than inside the exe.
    with open(os.path.join(GEN, "product.json"), "w", encoding="utf-8") as fh:
        json.dump({"name": p.name, "blurb": p.blurb, "panels": list(p.panels)},
                  fh, indent=2)
        fh.write("\n")

    # Version resource. fill() handles PRODUCT/EXE; BLURB is only used here.
    with open(TEMPLATE, "r", encoding="utf-8") as fh:
        res = p.fill(fh.read()).replace("{{BLURB}}", p.blurb)
    left = [t for t in ("{{PRODUCT}}", "{{EXE}}", "{{BLURB}}") if t in res]
    if left:
        sys.stderr.write("gen_build_files: %s left unsubstituted in %s\n"
                         % (" ".join(left), TEMPLATE))
        return 2
    version_file = os.path.join("gen", "%s.version.txt" % p.exe)
    with open(os.path.join(ROOT, version_file), "w", encoding="utf-8") as fh:
        fh.write(res)

    # Relative paths only: an absolute --add-data confuses PyInstaller when the
    # tree sits on a mapped UNC drive (see the note in build_console_exe.bat).
    env = {
        "PRODUCT": p.name,
        "EXE": p.exe,
        "VERSION_FILE": version_file.replace("/", os.sep),
        "HAS_WAVEFORM": "1" if p.has("waveform") else "0",
    }
    with open(os.path.join(GEN, "build.env"), "w", encoding="utf-8") as fh:
        for k in sorted(env):
            fh.write("%s=%s\n" % (k, env[k]))

    print("product : %s" % p.name)
    print("exe     : %s" % p.exe)
    print("panels  : %s" % (", ".join(p.panels) or "none (terminal only)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
