"""Score jobs with Claude in an interactive session instead of OpenRouter.

  python scripts/agent_scoring.py export [--limit N]      # unscored jobs -> logs/agent_batch_<n>.json
  (Claude reads the batch and writes logs/agent_eval_<n>.json: a list of submit_evaluation objects,
   each with a "job_id" key and the same fields score_jobs.EVAL_TOOL defines)
  python scripts/agent_scoring.py import logs/agent_eval_<n>.json

Import runs the normal pipeline on those jobs (screening, score_jobs.compute_evaluation with every
code-side guard, the > 9.0 top-match bar, jobs.xlsx updates, and the LinkedIn open check), with a
client that returns the precomputed evaluations instead of calling a model. Scheduled runs can't use
this; jobs they find stay "unscored" until scored here or by OpenRouter.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from excel_store import JobStore
from openrouter import DailyLimitReached
from run_pipeline import TO_SCORE, discovery_details, job_from_row, run, with_extras
from score_jobs import EVAL_TOOL
from verify_jobs import screen
from workspace import get_workspace

REPO = Path(__file__).resolve().parent.parent
DESC_CHARS = 3500
REQUIRED = EVAL_TOOL["function"]["parameters"]["required"]

log = logging.getLogger("agent_scoring")


class PrecomputedClient:
    """Stands in for OpenRouterClient; score_jobs.score_job() calls evaluation_for(job)."""
    model = "claude-in-session"

    def __init__(self, evaluations: dict[str, dict]):
        self.evaluations = evaluations

    def evaluation_for(self, job: dict) -> dict:
        ev = self.evaluations.get(job["job_id"])
        if ev is None:
            raise DailyLimitReached(f"no precomputed evaluation for {job['job_id']}")
        return ev


def _compact(text: str | None) -> str:
    text = re.sub(r"[ \t]+", " ", text or "")
    return re.sub(r"\n\s*\n+", "\n", text).strip()[:DESC_CHARS]


def export(limit: int | None) -> Path | None:
    ws = get_workspace().setup()
    store = JobStore(ws.jobs_xlsx)
    rows = [r for r in store.all() if r.get("Processing Status") in TO_SCORE]
    details = discovery_details(ws, [])
    batch = []
    for r in rows:
        job = with_extras(job_from_row(r), details)
        if screen(job):        # excluded at import time anyway; don't spend effort reading it
            continue
        batch.append({"job_id": job["job_id"], "title": job.get("title"), "company": job.get("company"),
                      "location": job.get("location"), "seniority": job.get("seniority_level"),
                      "experience_required": job.get("experience_required"),
                      "employment_type": job.get("employment_type"), "description": _compact(job.get("description"))})
        if limit and len(batch) >= limit:
            break
    if not batch:
        print("nothing to score")
        return None
    n = len(list(ws.logs_dir.glob("agent_batch_*.json"))) + 1
    out = ws.logs_dir / f"agent_batch_{n}.json"
    out.write_text(json.dumps(batch, indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(batch)} jobs -> {out}")
    return out


def load_evaluations(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    evals = {}
    for ev in data:
        missing = [k for k in REQUIRED if k not in ev]
        if missing or not ev.get("job_id"):
            raise ValueError(f"evaluation for {ev.get('job_id')!r} missing fields: {missing}")
        evals[str(ev["job_id"])] = {k: v for k, v in ev.items() if k != "job_id"}
    return evals


def import_(path: Path, rescore: bool = False) -> dict:
    ws = get_workspace()
    evals = load_evaluations(path)
    if rescore:  # corrected evaluations replace earlier scores (e.g. a lenient model's 9.8)
        store = JobStore(ws.jobs_xlsx)
        for jid in evals:
            row = store.get(jid)
            if row and row.get("Processing Status") not in TO_SCORE:
                store.upsert(jid, {"Processing Status": "discovered",
                                   "Error Details": f"re-scored (was {row.get('Match Score')}, {row.get('Processing Status')})"})
        store.save()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-agent-" + uuid.uuid4().hex[:4]
    client = PrecomputedClient(evals)
    stats = run(ws, run_id=run_id, client_factory=lambda budget: client, apify_token=os.environ.get("APIFY_API_TOKEN"),
                skip_discovery=True, job_ids=list(evals), tailor=False)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--limit", type=int)
    i = sub.add_parser("import")
    i.add_argument("file", type=Path)
    i.add_argument("--rescore", action="store_true", help="replace scores of jobs that were already scored")
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO / ".env")
    except ImportError:
        pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    if args.cmd == "export":
        export(args.limit)
        return 0
    stats = import_(args.file, rescore=args.rescore)
    keys = ("evaluated", "qualified", "rejected_low_fit", "excluded_staffing", "excluded_red_flag",
            "excluded_experience", "top_matches_new", "verified_open", "closed", "open_unconfirmed",
            "verified_top_matches_total", "errors")
    print(json.dumps({k: stats.get(k) for k in keys}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
