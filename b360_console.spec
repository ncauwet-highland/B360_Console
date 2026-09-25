# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['b360_console_app.py'],
    pathex=[],
    binaries=[],
    datas=[('b360_console.html', '.'), ('b360_wave_viewer.html', '.')],
    hiddenimports=['b360_link', 'b360_capture', 'serial', 'serial.tools.list_ports'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='b360_console',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='b360_console.version.txt',
    icon=['b360_console.ico'],
)
