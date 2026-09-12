@echo off
setlocal
rem Launch the widget runner with a windowless Python. Tries, in order:
rem   1) C:\Python3 (common local install)   2) pythonw on PATH
rem   3) the py launcher, windowless (pyw)    4) py -3 (may flash a console)
rem Edit the path in step 1 if your Python lives elsewhere.

if exist "C:\Python3\pythonw.exe" (
    "C:\Python3\pythonw.exe" "%~dp0runner.py" %*
    goto :eof
)

where pythonw >nul 2>&1
if %errorlevel%==0 (
    pythonw "%~dp0runner.py" %*
    goto :eof
)

where pyw >nul 2>&1
if %errorlevel%==0 (
    pyw -3 "%~dp0runner.py" %*
    goto :eof
)

where py >nul 2>&1
if %errorlevel%==0 (
    py -3 "%~dp0runner.py" %*
    goto :eof
)

echo Python 3.11+ not found. Install it or edit run.cmd with the path to pythonw.exe. 1>&2
exit /b 1
