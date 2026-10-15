"""Daily entry point for Windows Task Scheduler (via scripts/run_daily.cmd).

  1. run_pipeline.py     LinkedIn + Indeed + Naukri -> screen -> score (OpenRouter) -> verify -> jobs.xlsx
  2. agent_autoscore.py  jobs OpenRouter couldn't score are scored by headless Claude Code

Everything is appended to <workspace>/logs/scheduler.log. `--check` only proves the launcher works.
The scheduled task runs this with pythonw.exe, so no console window appears (closing one kills the run).
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

from workspace import get_workspace  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    logs = get_workspace().logs_dir
    logs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    with open(logs / "scheduler.log", "a", encoding="utf-8") as log:
        def note(msg: str) -> None:
            log.write(f"===== {datetime.now():%Y-%m-%d %H:%M:%S} {msg} =====\n")
            log.flush()

        if "--check" in argv:
            note(f"launcher check ok (python {sys.executable})")
            return 0
        code = 0
        extra = [a for a in argv if a not in ("--check", "--score-only")]   # e.g. --from-snapshot <file>
        steps = [("pipeline", ["run_pipeline.py", *extra]),
                 ("claude-fallback", ["agent_autoscore.py", "--batch", "40", "--max-batches", "15"])]
        if "--score-only" in argv:   # just score the backlog (on-demand task "JobApplicationAgent Score")
            steps = steps[1:]
        for name, args in steps:
            note(f"{name} start")
            proc = subprocess.run([sys.executable, str(SCRIPTS / args[0]), *args[1:]], cwd=SCRIPTS.parent,
                                  stdout=log, stderr=subprocess.STDOUT, env=env,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))   # no console window to close by accident
            note(f"{name} exit {proc.returncode}")
            code = code or proc.returncode
        return code


if __name__ == "__main__":
    sys.exit(main())
