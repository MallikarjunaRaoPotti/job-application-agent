@echo off
rem Daily job run, launched by Windows Task Scheduler ("JobApplicationAgent Daily").
rem Finds Python and runs scripts\daily.py, which does the pipeline + Claude fallback and logs to
rem <Desktop>\JobApplicationAgent\logs\scheduler.log. Extra arguments are passed through (e.g. --check).
setlocal
cd /d "%~dp0.."

rem Python: PYTHON_EXE override, else the newest per-user install, else "python" on PATH
set "PY=%PYTHON_EXE%"
if not defined PY for /d %%P in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%~P\python.exe" set "PY=%%~P\python.exe"
if not defined PY set "PY=python"

"%PY%" "%~dp0daily.py" %*
endlocal
