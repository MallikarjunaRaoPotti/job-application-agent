import shutil
import sys
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent / "scripts"))
FIXTURE_RESUME = TESTS / "fixtures" / "resume.md"


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    """Every test gets its own JobApplicationAgent workspace; the real Desktop is never touched."""
    root = tmp_path / "JobApplicationAgent"
    root.mkdir()
    shutil.copy(FIXTURE_RESUME, root / "resume.md")
    monkeypatch.setenv("JOB_AGENT_HOME", str(root))
    return root
