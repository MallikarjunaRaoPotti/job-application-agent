@echo off
rem Daily job-discovery run, launched by Windows Task Scheduler ("JobApplicationAgent Daily").
rem 1) pipeline: LinkedIn + Indeed + Naukri -> screen -> score (OpenRouter) -> verify -> jobs.xlsx
rem 2) fallback: jobs OpenRouter couldn't score (no credits / budget) are scored by headless Claude Code
rem Output is appended to <Desktop>\JobApplicationAgent\logs\scheduler.log (the pipeline also writes per-run logs).
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8

rem Python: PYTHON_EXE override, else the newest per-user install, else whatever "python" is on PATH
set "PY=%PYTHON_EXE%"
if not defined PY for /d %%P in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%P\python.exe" set "PY=%%P\python.exe"
if not defined PY set "PY=python"

for /f "delims=" %%D in ('"%PY%" -c "import sys; sys.path.insert(0, 'scripts'); from workspace import get_workspace; print(get_workspace().logs_dir)"') do set "LOGDIR=%%D"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
echo ===== %date% %time% start ===== >> "%LOGDIR%\scheduler.log"
"%PY%" scripts\run_pipeline.py >> "%LOGDIR%\scheduler.log" 2>&1
echo ===== %date% %time% pipeline exit %errorlevel% ===== >> "%LOGDIR%\scheduler.log"
"%PY%" scripts\agent_autoscore.py --batch 40 --max-batches 15 >> "%LOGDIR%\scheduler.log" 2>&1
echo ===== %date% %time% claude-fallback exit %errorlevel% ===== >> "%LOGDIR%\scheduler.log"
endlocal
