"""Local workspace paths: <Desktop>/JobApplicationAgent/ holds all data, the code stays in the repo.

  JobApplicationAgent/
    PRD.md, resume.md (base resume, read-only to the pipeline), jobs.xlsx
    output/resumes/<job_id>/resume.md + tailoring_report.md
    logs/      run logs, per-run JSON snapshots, failed resume drafts
    config/    search.json, search_urls.txt

The Desktop is resolved from the home directory at runtime (OneDrive-redirected Desktops included);
JOB_AGENT_HOME overrides the whole workspace location (used by tests).
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKSPACE_NAME = "JobApplicationAgent"


def desktop_dir(home: Path | None = None) -> Path:
    home = home or Path.home()
    candidates = [home / "Desktop", *sorted(home.glob("OneDrive*/Desktop"))]
    for c in candidates:
        if c.is_dir():
            return c
    return home / "Desktop"


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def resume(self) -> Path:
        return self.root / "resume.md"

    @property
    def prd(self) -> Path:
        return self.root / "PRD.md"

    @property
    def jobs_xlsx(self) -> Path:
        return self.root / "jobs.xlsx"

    @property
    def resumes_dir(self) -> Path:
        return self.root / "output" / "resumes"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    def job_dir(self, job_id: str) -> Path:
        if not job_id or not job_id.isalnum():
            raise ValueError(f"unsafe job_id for a folder name: {job_id!r}")
        return self.resumes_dir / job_id

    def setup(self) -> "Workspace":
        """Create folders and seed missing files from the repo. Never overwrites existing files."""
        for d in (self.resumes_dir, self.logs_dir, self.config_dir):
            d.mkdir(parents=True, exist_ok=True)
        seeds = {self.prd: REPO / "PRD.md",
                 self.config_dir / "search.json": REPO / "config" / "search.json",
                 self.config_dir / "search_urls.txt": REPO / "config" / "search_urls.txt"}
        for target, source in seeds.items():
            if not target.exists() and source.exists():
                shutil.copy2(source, target)
        if not self.resume.exists():
            raise FileNotFoundError(f"base resume not found: {self.resume}")
        return self


def get_workspace() -> Workspace:
    if os.environ.get("JOB_AGENT_HOME"):
        return Workspace(Path(os.environ["JOB_AGENT_HOME"]))
    return Workspace(desktop_dir() / WORKSPACE_NAME)
