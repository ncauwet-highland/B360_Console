#!/usr/bin/env bash
# Build a standalone console AppImage for one product -- the Linux counterpart
# of build_console_exe.bat.
#
#   ./build_console_appimage.sh          ->  dist/b360_console.AppImage
#   ./build_console_appimage.sh B960     ->  dist/b960_console.AppImage, terminal only
#
# The product profile drives everything exactly as it does for the .exe: window
# titles, the captures folder, the capture file prefix, the file name, and which
# optional panels are bundled. tools/gen_build_files.py is shared by both builds.
#
# Python 3.9+ (with venv) is needed on THIS machine only. The AppImage carries its
# own interpreter, its pages, the capture code and Qt WebEngine for the window,
# so the target needs nothing installed: chmod +x and run it.
#
# Why Qt and not GTK: pywebview can use either on Linux, but WebKitGTK comes
# from the distribution and cannot be bundled reliably, so a GTK build would only
# run where the right libwebkit2gtk happens to be installed. Qt WebEngine comes
# from pip wheels and bundles whole. The price is size (~150 MB+).
#
# Build on the OLDEST Ubuntu/Debian the AppImage must run on: it needs a glibc at
# least as new as the build machine's.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

fail() { echo; echo "BUILD FAILED: $*" >&2; exit 1; }
trap 'fail "line $LINENO"' ERR

PRODUCT_ARG="${1:-B360}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/b360_console_build"
VENV="$CACHE/venv"
ARCH="$(uname -m)"
APPIMAGETOOL="$CACHE/appimagetool-$ARCH.AppImage"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-$ARCH.AppImage"

mkdir -p "$CACHE"
if [ ! -x "$VENV/bin/python" ]; then
  echo "Creating build venv in $VENV"
  python3 -m venv "$VENV" || fail "python3 -m venv failed (sudo apt install python3-venv)"
fi
PY="$VENV/bin/python"
# pywebview[qt] pulls in QtPy, PyQt6 and PyQt6-WebEngine. Pillow is only for
# turning console.ico into the PNG an AppImage wants as its icon.
"$PY" -m pip install --quiet --upgrade pip pyinstaller "pywebview[qt]" pyserial pillow

# Same generator as the Windows build. It also writes a Windows version
# resource, which this build simply does not use.
"$PY" tools/gen_build_files.py "$PRODUCT_ARG"

# Read the derived values back: EXE, PRODUCT, HAS_WAVEFORM.
EXE= PRODUCT= HAS_WAVEFORM=
while IFS='=' read -r k v; do
  case "$k" in EXE|PRODUCT|HAS_WAVEFORM) printf -v "$k" '%s' "${v%$'\r'}";; esac
done < gen/build.env
[ -n "$EXE" ] || fail "could not read gen/build.env"

WF_ARGS=()
if [ "$HAS_WAVEFORM" = "1" ]; then
  WF_ARGS=(--add-data "$PWD/b360_wave_viewer.html:." --hidden-import b360_capture)
fi

# --onedir, not --onefile: the AppImage is already a single compressed file,
# and a onefile bundle inside it would unpack ~150 MB to /tmp on every launch.
# Work and spec paths are kept apart from the Windows build's; --add-data is
# resolved against --specpath, hence the absolute sources.
"$PY" -m PyInstaller \
  --onedir --windowed --clean --noconfirm \
  --name "$EXE" \
  --distpath build/linux/dist --workpath build/linux/work --specpath build/linux \
  --add-data "$PWD/b360_console.html:." \
  --add-data "$PWD/gen/product.json:." \
  "${WF_ARGS[@]}" \
  --hidden-import b360_link --hidden-import product \
  --hidden-import serial --hidden-import serial.tools.list_ports \
  --hidden-import webview.platforms.qt \
  --exclude-module tkinter --exclude-module unittest --exclude-module pydoc \
  --exclude-module gi \
  b360_console_app.py

BUNDLE="build/linux/dist/$EXE"
"$BUNDLE/$EXE" --selftest || fail "self-test of the PyInstaller bundle failed"

# ---------------------------------------------------------------- AppDir ---
APPDIR="build/linux/$EXE.AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/lib"
cp -a "$BUNDLE" "$APPDIR/usr/lib/$EXE"

# PyInstaller's PyQt6 hook collects all of QML and every Qt translation. The
# window is a QWebEngineView in a QMainWindow: WebEngine links Quick and Qml (so
# those stay), but nothing loads Quick3D, the QuickControls2 styles, PDF, the
# QML import tree, or any translation. Chromium's locale pak falls back to en-US.
QT="$APPDIR/usr/lib/$EXE/_internal/PyQt6/Qt6"
if [ -d "$QT" ]; then
  rm -rf "$QT/qml"
  rm -f "$QT"/lib/libQt6Quick3D* "$QT"/lib/libQt6QuickControls2* \
        "$QT"/lib/libQt6QuickDialogs2* "$QT"/lib/libQt6QuickTemplates2* \
        "$QT"/lib/libQt6ShaderTools* "$QT"/lib/libQt6Pdf*
  find "$QT/translations" -maxdepth 1 -name '*.qm' -delete
  find "$QT/translations/qtwebengine_locales" -name '*.pak' ! -name 'en-US.pak' -delete
fi

"$PY" - "$APPDIR/$EXE.png" <<'EOF'
import sys
from PIL import Image
im = Image.open("console.ico")
best = max(im.ico.sizes(), key=lambda s: s[0] * s[1])
im.size = best
im.load()
im.convert("RGBA").resize((256, 256), Image.LANCZOS).save(sys.argv[1])
EOF
ln -s "$EXE.png" "$APPDIR/.DirIcon"

cat > "$APPDIR/$EXE.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$PRODUCT Console
Comment=$PRODUCT terminal$( [ "$HAS_WAVEFORM" = "1" ] && echo " and waveform viewer" )
Exec=$EXE %F
Icon=$EXE
Terminal=false
Categories=Development;Electronics;
EOF

cat > "$APPDIR/AppRun" <<EOF
#!/bin/sh
HERE="\$(dirname "\$(readlink -f "\$0")")"
# Use the bundled Qt backend; there is no GTK/WebKit in the bundle.
export PYWEBVIEW_GUI="\${PYWEBVIEW_GUI:-qt}"
# Chromium's sandbox needs unprivileged user namespaces, which Ubuntu 23.10+
# denies through AppArmor to programs it has no profile for -- an AppImage is
# exactly that, and the window would come up blank. The pages it loads are its
# own, from inside the bundle, so the sandbox is not guarding anything here.
export QTWEBENGINE_DISABLE_SANDBOX="\${QTWEBENGINE_DISABLE_SANDBOX:-1}"
exec "\$HERE/usr/lib/$EXE/$EXE" "\$@"
EOF
chmod +x "$APPDIR/AppRun"

# ---------------------------------------------------------- appimagetool ---
if [ ! -x "$APPIMAGETOOL" ]; then
  echo "Downloading appimagetool to $APPIMAGETOOL"
  curl -fL --retry 3 -o "$APPIMAGETOOL.part" "$APPIMAGETOOL_URL" \
    || fail "could not download appimagetool"
  chmod +x "$APPIMAGETOOL.part"
  mv "$APPIMAGETOOL.part" "$APPIMAGETOOL"
fi

mkdir -p dist
OUT="dist/$EXE.AppImage"
# Extract-and-run so the build works without FUSE (WSL, containers, CI).
APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" "$APPIMAGETOOL" --no-appstream "$APPDIR" "$OUT"

echo
echo "Built $OUT  ($PRODUCT)"
APPIMAGE_EXTRACT_AND_RUN=1 "./$OUT" --selftest || fail "self-test of the AppImage failed"
echo "Self-test passed."

# The AppImage ships unsigned; this hash is how a recipient checks they have
# the file that was built here.
( cd dist && sha256sum "$EXE.AppImage" > "$EXE.AppImage.sha256" && cat "$EXE.AppImage.sha256" )

echo
echo "Run \"$OUT --smoke 5\" to open it briefly."
