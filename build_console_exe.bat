@echo off
REM Build a standalone console executable for one product.
REM
REM   build_console_exe.bat            ->  B360 (the default)
REM   build_console_exe.bat B960       ->  dist\b960_console.exe, terminal only
REM
REM The product name selects products\<name>.json, and everything the operator
REM sees is derived from it -- window titles, the captures folder, the capture
REM file prefix, the exe name and its version resource. Which optional GUI panels
REM the build carries comes from that profile too, so a terminal-only product does
REM not ship a page it cannot open.
REM
REM Python 3.9+ is needed on THIS machine only. The .exe it produces carries its
REM own interpreter, its pages and the capture code, and opens as an application
REM window via the WebView2 control already present in Windows.
REM
REM pushd (not cd) so this also works from a WSL \\wsl.localhost path, which cmd
REM cannot make a current directory without mapping a drive letter first.
setlocal enabledelayedexpansion
pushd "%~dp0" || goto :fail

set PRODUCT_ARG=%~1
if "%PRODUCT_ARG%"=="" set PRODUCT_ARG=B360

set VENV=%TEMP%\b360_viewer_venv
if not exist "%VENV%\Scripts\python.exe" (
  echo Creating build venv in %VENV%
  py -3 -m venv "%VENV%" || goto :fail
)
"%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip pyinstaller pywebview pyserial || goto :fail

REM Generate product.json, the version resource and build.env into gen\ -- NOT
REM build\, which is PyInstaller's workpath and which --clean deletes. Fails
REM loudly on an unknown product name rather than quietly building the default.
"%VENV%\Scripts\python.exe" tools\gen_build_files.py "%PRODUCT_ARG%" || goto :fail

REM Read the derived values back: EXE, PRODUCT, VERSION_FILE, HAS_WAVEFORM.
for /f "usebackq tokens=1,* delims==" %%a in ("gen\build.env") do set "%%a=%%b"
if "%EXE%"=="" echo Could not read gen\build.env & goto :fail

REM The viewer page and the capture module ship only with the panel that needs
REM them; --selftest on a terminal-only build does not look for them either.
set WF_ARGS=
if "%HAS_WAVEFORM%"=="1" set WF_ARGS=--add-data "b360_wave_viewer.html;." --hidden-import b360_capture

REM Paths stay relative: an absolute --distpath/--specpath confuses --add-data
REM when the source tree is on a mapped UNC drive.
REM The capture modules are imported inside functions, so name them explicitly
REM rather than trusting the import scan to reach them.
"%VENV%\Scripts\python.exe" -m PyInstaller ^
  --onefile --noconsole --clean --noconfirm ^
  --name %EXE% ^
  --icon console.ico ^
  --version-file "%VERSION_FILE%" ^
  --add-data "b360_console.html;." ^
  --add-data "gen\product.json;." ^
  %WF_ARGS% ^
  --hidden-import b360_link --hidden-import product ^
  --hidden-import serial --hidden-import serial.tools.list_ports ^
  --exclude-module tkinter --exclude-module unittest --exclude-module pydoc ^
  b360_console_app.py || goto :fail

echo.
echo Built dist\%EXE%.exe  (%PRODUCT%)
dist\%EXE%.exe --selftest || goto :fail
echo Self-test passed.

REM The executable ships unsigned; this hash is how a recipient checks they have
REM the file that was built here.
powershell -NoProfile -Command "$f='%~dp0dist\%EXE%.exe'; $h=(Get-FileHash $f -Algorithm SHA256).Hash.ToLower(); ($h + '  %EXE%.exe') | Set-Content ($f + '.sha256') -Encoding ascii; 'SHA-256 : ' + $h"
if errorlevel 1 echo WARNING: could not write the checksum; the build itself is fine.

echo.
echo Run "dist\%EXE%.exe --smoke 5" to open it briefly.
popd
goto :eof

:fail
echo.
echo BUILD FAILED
popd
pause
exit /b 1
