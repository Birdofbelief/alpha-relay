@echo off
REM ============================================================
REM  alpha-relay -- double-clickable demo launcher
REM
REM  Why this file exists: double-clicking a .py on Windows opens a
REM  console, runs it, and closes the window the instant the process
REM  exits -- so you never see the output, whether it succeeded or
REM  failed. This wrapper runs the demo and then WAITS for a keypress.
REM
REM  ASCII-only on purpose. A .cmd containing non-ASCII text is a
REM  codepage minefield (GBK vs UTF-8): cmd.exe can mis-decode it and
REM  end up executing mojibake as commands, which shows up as
REM  "double-click did nothing". Staying ASCII sidesteps all of it.
REM  (The Chinese output comes from Python, which handles its own
REM  stdout encoding correctly.)
REM ============================================================

REM cd to this file's own folder, so it works no matter where the
REM user copied the repo to. %~dp0 always ends with a backslash.
cd /d "%~dp0"

echo.
echo ============================================================
echo   alpha-relay  --  demo
echo ============================================================
echo.

REM Prefer the Windows Python launcher. Fall back to `python` if the
REM launcher is not installed.
where py >nul 2>nul
if errorlevel 1 (
    set "PYEXE=python"
) else (
    set "PYEXE=py"
)

"%PYEXE%" -m signalchain.demo
set "RC=%ERRORLEVEL%"

if not "%RC%"=="0" goto failed

echo.
echo ------------------------------------------------------------
echo   Demo finished OK.   (exit code 0)
echo ------------------------------------------------------------
echo.
echo Press any key to close this window.
pause >nul
exit /b 0

:failed
echo.
echo ------------------------------------------------------------
echo   The demo did NOT run.   (exit code %RC%)
echo ------------------------------------------------------------
echo.
echo   Common causes:
echo.
echo    1. Python not found.
echo       Install it from https://www.python.org/downloads/ and tick
echo       "Add python.exe to PATH" during setup.
echo.
echo    2. Wrong folder. This file must sit in the repository root,
echo       right next to the "signalchain" folder. (It does try to cd
echo       to its own folder first -- so if you copied this file out
echo       of the repo, that is the problem.)
echo.
echo    3. Missing dependency. Try:
echo         %PYEXE% -m pip install requests pandas
echo.
echo   You can also run the demo by hand, from the repo root:
echo.
echo       %PYEXE% -m signalchain.demo
echo.
echo ------------------------------------------------------------
echo.
echo Press any key to close this window.
pause >nul
exit /b 1
