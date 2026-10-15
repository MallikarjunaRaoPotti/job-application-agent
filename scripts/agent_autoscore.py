"""Automatic scoring fallback: when OpenRouter can't score (no credits / budget), headless Claude Code does.

Loop: agent_scoring.export (batch of unscored jobs) -> `claude -p` writes the evaluations -> import
(all code guards, the 9.0 bar and the LinkedIn open check still apply). Nothing to score -> no-op,
so it is safe to run after every pipeline run (scripts/run_daily.cmd does).

The Claude Code binary is found via CLAUDE_BIN, PATH, or the newest VS Code extension install.

Usage:
  python scripts/agent_autoscore.py                 # up to 10 batches of 40
  python scripts/agent_autoscore.py --batch 30 --max-batches 3
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_scoring  # noqa: E402
from score_jobs import EVAL_TOOL  # noqa: E402
from workspace import get_workspace  # noqa: E402

log = logging.getLogger("agent_autoscore")

PROMPT = """You are scoring job postings for the candidate whose base resume is at {resume}.
Read the batch file {batch} (a JSON list of jobs) and the resume. Then WRITE the file {out}: a JSON list
with exactly one object per job in the batch, each object having "job_id" plus every field of the
submit_evaluation tool below. Follow the scoring rules in CLAUDE.md ("In-session scoring") strictly:

- Be strict and evidence-based. The candidate is a 2026 fresher with ~4 months of internship.
- skills_match / experience_fit / project_relevance / role_alignment are integers 0-10.
- matched_skills: [{{"skill": ..., "resume_evidence": ...}}] where resume_evidence is a SHORT phrase copied
  VERBATIM from the resume (e.g. "PySpark, Databricks, Delta Lake"). No verbatim evidence -> not matched.
- missing_skills: core requirements the resume doesn't show. Soft skills don't count either way.
- min_years_required: the minimum years the posting requires (number) or null. Read the description:
  "2+ years", "2 to 4 years", "1-3 yrs" etc. Master's-only, Tier-1-only, or students-only eligibility
  means a low experience_fit and must be said in experience_match.
- role_alignment: high only for data engineering, data/BI analytics, analytics engineering, AI/GenAI/
  agentic AI engineering, or Python roles with data/AI work; marketing, sales, finance, ops, support -> low.
- required_skills / preferred_skills: as the posting states them (short names), else [].
- Keep reasoning to 1-3 sentences.

submit_evaluation fields: {fields}

Write only that JSON file (no other files), then reply DONE."""


def claude_bin() -> str | None:
    if os.environ.get("CLAUDE_BIN"):
        return os.environ["CLAUDE_BIN"]
    found = shutil.which("claude")
    if found:
        return found
    exts = sorted((Path.home() / ".vscode" / "extensions").glob("anthropic.claude-code-*"),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    for ext in exts:
        for name in ("claude.exe", "claude"):
            cand = ext / "resources" / "native-binary" / name
            if cand.is_file():
                return str(cand)
    return None


def score_batch(binary: str, batch: Path, out: Path, resume: Path, timeout: int) -> bool:
    """One headless Claude Code run; may only read files and write inside the workspace logs folder."""
    fields = json.dumps(EVAL_TOOL["function"]["parameters"]["properties"])
    prompt = PROMPT.format(resume=resume, batch=batch, out=out, fields=fields)
    cmd = [binary, "-p", prompt, "--max-turns", "12", "--add-dir", str(out.parent), "--add-dir", str(resume.parent),
           "--permission-mode", "acceptEdits", "--allowedTools", "Read", "Write"]
    # stdin=DEVNULL: `claude -p` waits for piped stdin to close; under Task Scheduler (pythonw) the
    # inherited stdin never closes, so without this the batch hangs forever.
    proc = subprocess.Popen(cmd, cwd=REPO, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace",
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        log.error("claude timed out on %s after %ds; killed", batch.name, timeout)
        return False
    if proc.returncode != 0:
        log.error("claude failed on %s: %s", batch.name, (stderr or stdout)[-300:])
        return False
    return out.is_file()


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the process and its children (a plain kill can leave a child holding the output pipes)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        proc.kill()
    try:
        proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--batch", type=int, default=40)
    parser.add_argument("--max-batches", type=int, default=10)
    parser.add_argument("--timeout", type=int, default=1200, help="seconds per batch")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO / ".env")
    except ImportError:
        pass

    binary = claude_bin()
    if not binary:
        log.error("Claude Code CLI not found (set CLAUDE_BIN); jobs stay unscored")
        return 1
    ws = get_workspace()
    totals = {"batches": 0, "evaluated": 0, "verified_top_matches_total": None, "failed_batches": 0}
    for _ in range(args.max_batches):
        batch = agent_scoring.export(args.batch)
        if batch is None:
            break
        out = batch.with_name(batch.name.replace("agent_batch_", "agent_eval_"))
        log.info("scoring %s with Claude Code -> %s", batch.name, out.name)
        if not score_batch(binary, batch, out, ws.resume, args.timeout):
            totals["failed_batches"] += 1
            break
        try:
            stats = agent_scoring.import_(out, strict=False)
        except (ValueError, json.JSONDecodeError) as exc:
            log.error("invalid evaluations in %s: %s", out.name, exc)
            totals["failed_batches"] += 1
            break
        totals["batches"] += 1
        totals["evaluated"] += stats.get("evaluated", 0)
        totals["verified_top_matches_total"] = stats.get("verified_top_matches_total")
        log.info("batch %s: %d evaluated, %d new top matches", batch.name, stats.get("evaluated", 0),
                 stats.get("top_matches_new", 0))
    print(json.dumps(totals, indent=1))
    return 1 if totals["failed_batches"] else 0


if __name__ == "__main__":
    sys.exit(main())
