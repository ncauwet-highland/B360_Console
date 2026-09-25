@echo off
REM Build the standalone B360 console executable.
REM
REM Python 3.9+ is needed on THIS machine only. The .exe it produces carries its
REM own interpreter, both pages and the capture code, and opens as an
REM application window via the WebView2 control already present in Windows.
REM
REM pushd (not cd) so this also works from a WSL \\wsl.localhost path, which cmd
REM cannot make a current directory without mapping a drive letter first.
setlocal
pushd "%~dp0" || goto :fail

set VENV=%TEMP%\b360_viewer_venv
if not exist "%VENV%\Scripts\python.exe" (
  echo Creating build venv in %VENV%
  py -3 -m venv "%VENV%" || goto :fail
)
"%VENV%\Scripts\python.exe" -m pip install --quiet --upgrade pip pyinstaller pywebview pyserial || goto :fail

REM Paths stay relative: an absolute --distpath/--specpath confuses --add-data
REM when the source tree is on a mapped UNC drive.
REM The capture modules are imported inside functions, so name them explicitly
REM rather than trusting the import scan to reach them.
"%VENV%\Scripts\python.exe" -m PyInstaller ^
  --onefile --noconsole --clean --noconfirm ^
  --name b360_console ^
  --icon b360_console.ico ^
  --version-file b360_console.version.txt ^
  --add-data "b360_console.html;." ^
  --add-data "b360_wave_viewer.html;." ^
  --hidden-import b360_link --hidden-import b360_capture ^
  --hidden-import serial --hidden-import serial.tools.list_ports ^
  --exclude-module tkinter --exclude-module unittest --exclude-module pydoc ^
  b360_console_app.py || goto :fail

echo.
echo Built dist\b360_console.exe
dist\b360_console.exe --selftest || goto :fail
echo Self-test passed.

REM The executable ships unsigned; this hash is how a recipient checks they have
REM the file that was built here.  See docs/b360_console_draft.md on why not signed.
powershell -NoProfile -Command "$f='%~dp0dist\b360_console.exe'; $h=(Get-FileHash $f -Algorithm SHA256).Hash.ToLower(); ($h + '  b360_console.exe') | Set-Content ($f + '.sha256') -Encoding ascii; 'SHA-256 : ' + $h"
if errorlevel 1 echo WARNING: could not write the checksum; the build itself is fine.

echo.
echo Run "dist\b360_console.exe --smoke 5" to open both windows briefly.
popd
goto :eof

:fail
echo.
echo BUILD FAILED
popd
pause
exit /b 1
