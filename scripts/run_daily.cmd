@echo off
rem Daily job-discovery run, launched by Windows Task Scheduler ("JobApplicationAgent Daily").
rem Output is appended to <Desktop>\JobApplicationAgent\logs\scheduler.log (the pipeline also writes per-run logs).
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set "PY=C:\Users\malli\AppData\Local\Programs\Python\Python310\python.exe"
if not exist "%PY%" set "PY=python"
for /f "delims=" %%D in ('"%PY%" -c "import sys; sys.path.insert(0, 'scripts'); from workspace import get_workspace; print(get_workspace().logs_dir)"') do set "LOGDIR=%%D"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
echo ===== %date% %time% start ===== >> "%LOGDIR%\scheduler.log"
"%PY%" scripts\run_pipeline.py >> "%LOGDIR%\scheduler.log" 2>&1
echo ===== %date% %time% exit %errorlevel% ===== >> "%LOGDIR%\scheduler.log"
endlocal
