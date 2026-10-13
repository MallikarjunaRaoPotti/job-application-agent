"""Local job pipeline: discover -> dedup -> screen -> score -> verify open -> jobs.xlsx [-> tailor].

Everything is stored under <Desktop>/JobApplicationAgent/ (see workspace.py). jobs.xlsx is both the
tracker and the dedup store, so reruns are safe: known jobs are skipped and unfinished work is retried.

Current phase: resume tailoring is PAUSED (opt in with --tailor). Jobs are screened for staffing
agencies and red flags before scoring; jobs scoring > 9.0 are re-checked as open on LinkedIn every run
and marked verified_top_match. Applications are never submitted.

Usage:
  python scripts/run_pipeline.py                     # discover, screen, score, verify
  python scripts/run_pipeline.py --skip-discovery    # only finish work already in jobs.xlsx
  python scripts/run_pipeline.py --dry-run           # call APIs, print results, write nothing
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from excel_store import JobStore, WorkbookLockedError, split_list
from openrouter import DEFAULT_MODEL, AuthError, DailyLimitReached, OpenRouterClient, is_free
from resume_parser import load_base_resume
from score_jobs import FIT_THRESHOLD, score_jobs
from scrape_jobs import ScrapeError, deduplicate, discover, load_search_config, read_urls_file
from sources import SOURCES, normalize_all, search_source
from tailor_job import process_job
from verify_jobs import check_open, screen
from views import source_of
from workspace import Workspace, get_workspace

REPO = Path(__file__).resolve().parent.parent
TO_SCORE = {"discovered", "unscored", "scoring_error"}
TO_TAILOR = {"qualified", "tailoring", "validation_failed", "tailoring_error"}
TO_VERIFY = {"top_match_unverified", "verified_top_match"}   # re-checked as open on every run
TOP_MATCH_THRESHOLD = 9.0   # strictly greater than
DEFAULT_SCORE_CALLS_FREE = 35    # of the 50/day free OpenRouter quota
DEFAULT_SCORE_CALLS_PAID = 600
DEFAULT_LLM_SPEND_USD = 1.00     # per run, paid model
DEFAULT_TAILOR_CALLS = 10
SCORE_WORKERS_PAID = 8

log = logging.getLogger("pipeline")


# ------------------------------------------------------------------ row <-> job

def _work_mode(value: str | None) -> str | None:
    v = (value or "").strip().lower().replace("-", "").replace(" ", "")
    return {"remote": "remote", "hybrid": "hybrid", "onsite": "onsite"}.get(v)


def discovery_row(job: dict, status: str) -> dict:
    return {
        "Source Reference ID": job.get("ref_id"), "Job Title": job.get("title"), "Company": job.get("company"),
        "Location": job.get("location"), "Job Type": job.get("employment_type"),
        "Work Mode": _work_mode(job.get("work_mode")), "Salary": job.get("salary"), "Job URL": job.get("link"),
        "Job Description": job.get("description"),
        "Required Skills": job.get("required_skills") or [],
        "Date Discovered": (job.get("first_seen_at") or "")[:10] or None,
        "Processing Status": status, "Error Details": None,
    }


def job_from_row(row: dict) -> dict:
    """Rebuild the job dict that scoring/tailoring expect from a jobs.xlsx row."""
    score = row.get("Match Score")
    job = {
        "job_id": str(row["Job ID"]), "ref_id": row.get("Source Reference ID"), "title": row.get("Job Title"),
        "company": row.get("Company"), "location": row.get("Location"), "employment_type": row.get("Job Type"),
        "work_mode": row.get("Work Mode"), "salary": row.get("Salary"), "link": row.get("Job URL"),
        "description": row.get("Job Description"), "source": "Apify",
    }
    if isinstance(score, (int, float)) and not isinstance(score, bool):
        fit = int(math.floor(score + 0.5))
        job["evaluation"] = {"fit_score": fit, "match_score": float(score), "matched_skills": split_list(row.get("Matched Skills")),
                             "missing_skills": split_list(row.get("Missing Skills")),
                             "qualification_status": "qualified" if fit >= FIT_THRESHOLD else "rejected"}
    if job["ref_id"] is not None:
        job["ref_id"] = str(job["ref_id"])
    return job


SCORING_EXTRAS = ("seniority_level", "experience_required", "required_skills", "posted_date", "first_seen_at",
                  "company_industry", "description_core_chars", "posted_at", "source")


def discovery_details(ws: Workspace, fresh: list[dict]) -> dict[str, dict]:
    """Full normalized records by job_id: this run's discovery plus earlier logs/*_discovery.json snapshots.

    jobs.xlsx has no seniority/experience columns, but the strict experience cap in scoring needs them.
    """
    from scrape_jobs import normalize
    details: dict[str, dict] = {}
    now = datetime.now(timezone.utc)
    for snap in sorted(ws.logs_dir.glob("*_discovery.json")) + sorted(ws.logs_dir.glob("*_source_*.json")):
        try:
            data = json.loads(snap.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for item in data.get("raw_items") or []:
            job = normalize(item, now) if isinstance(item, dict) else None
            if job:
                details[job["job_id"]] = job
        for job in data.get("jobs") or []:          # Indeed / Naukri snapshots
            if isinstance(job, dict) and job.get("job_id"):
                details[job["job_id"]] = job
    details.update({j["job_id"]: j for j in fresh})
    return details


def with_extras(job: dict, details: dict[str, dict]) -> dict:
    extra = details.get(job["job_id"])
    if not extra:
        return job
    out = {**job, **{k: extra[k] for k in SCORING_EXTRAS if extra.get(k) is not None}}
    out["company_linkedin_url"] = extra.get("company_linkedin_url")  # None is meaningful (red flag)
    return out


class _SkipLinkedIn(Exception):
    pass


def discover_sources(cfg: dict, token: str | None, index: dict, ws: Workspace, run_id: str, stats: dict,
                     upsert, dry_run: bool, source_items: dict | None = None) -> list[dict]:
    """Indeed / Naukri: last-24h keyword searches -> normalize -> dedup against jobs.xlsx -> rows."""
    fresh = []
    now = datetime.now(timezone.utc)
    for name, scfg in (cfg.get("sources") or {}).items():
        if name not in SOURCES or not scfg.get("enabled", True):
            continue
        queries = scfg.get("queries") or cfg.get("queries") or []
        try:
            if source_items is not None:
                items = source_items.get(name, [])
            elif token:
                items = search_source(name, queries, scfg, token)
            else:
                raise ScrapeError("APIFY_API_TOKEN is not set")
        except ScrapeError as exc:
            log.error("[%s] %s discovery failed: %s", run_id, name, exc)
            stats["errors"].append({"stage": f"discovery:{name}", "error": str(exc)})
            continue
        jobs, malformed = normalize_all(name, items, now)
        eligible, buckets = deduplicate(jobs, index)
        for job in eligible:
            upsert(job["job_id"], discovery_row(job, "discovered"))
        for job in buckets["expired"]:
            upsert(job["job_id"], discovery_row(job, "expired"))
        stats["sources"][name] = {"found": len(items), "malformed": malformed, "normalized": len(jobs),
                                  "duplicate_in_run": len(buckets["duplicate_in_run"]),
                                  "previously_seen": len(buckets["previously_seen"]),
                                  "expired": len(buckets["expired"]), "eligible": len(eligible)}
        log.info("[%s] %s: %d found -> %d normalized -> %d new eligible (%d seen before, %d expired)", run_id,
                 name, len(items), len(jobs), len(eligible), len(buckets["previously_seen"]), len(buckets["expired"]))
        if not dry_run:
            (ws.logs_dir / f"{run_id}_source_{name}.json").write_text(json.dumps(
                {"run_id": run_id, "source": name, "stats": stats["sources"][name], "jobs": jobs},
                indent=1, ensure_ascii=False, default=str), encoding="utf-8")
        fresh += eligible
    return fresh


def _needs_tailoring(row: dict, force: bool) -> bool:
    score = row.get("Match Score")
    if not isinstance(score, (int, float)) or isinstance(score, bool) or math.floor(score + 0.5) < FIT_THRESHOLD:
        return False
    status = row.get("Processing Status")
    if force or status in TO_TAILOR:
        return True
    # A finished job whose resume file was deleted gets regenerated.
    return status == "ready_for_review" and not (row.get("Resume Path") and Path(row["Resume Path"]).is_file())


# ------------------------------------------------------------------ pipeline

def run(ws: Workspace, *, run_id: str, client_factory: Callable[[int], OpenRouterClient],
        apify_token: str | None = None, raw_items: list[dict] | None = None, search_config: dict | None = None,
        skip_discovery: bool = False, job_ids: list[str] | None = None, force: bool = False,
        dry_run: bool = False, score_calls: int = DEFAULT_SCORE_CALLS_FREE, score_workers: int = 1,
        tailor: bool = False, tailor_calls: int = DEFAULT_TAILOR_CALLS,
        open_checker: Callable[[list[str]], dict] | None = None,
        search_hits: list[dict] | None = None, source_items: dict[str, list[dict]] | None = None,
        only_sources: list[str] | None = None) -> dict:
    ws.setup()
    base = load_base_resume(ws.resume)
    store = JobStore(ws.jobs_xlsx)
    stats = {"run_id": run_id, "rows_before": len(store), "rows_inserted": 0, "rows_updated": 0,
             "discovery": None, "evaluated": 0, "qualified": 0, "rejected_low_fit": 0, "unscored": 0,
             "scoring_errors": 0, "excluded_staffing": 0, "excluded_red_flag": 0, "excluded_experience": 0, "top_matches_new": 0,
             "verified_open": 0, "closed": 0, "open_unconfirmed": 0, "verified_top_matches_total": 0,
             "sources": {}, "tailoring": "enabled" if tailor else "paused", "resumes_generated": 0,
             "resumes_skipped_existing": 0, "validation_failed": 0, "tailoring_deferred": 0, "errors": []}
    if open_checker is None and apify_token:
        def open_checker(ids):
            return check_open(ids, apify_token)

    def save():
        if not dry_run:
            store.save()

    def upsert(job_id: str, values: dict) -> None:
        if dry_run:
            return
        action = store.upsert(job_id, values)
        stats[f"rows_{action}"] += 1

    # 1-3. discover, normalize, dedup against jobs.xlsx
    if not skip_discovery:
        cfg = search_config if search_config is not None else load_search_config(ws.config_dir / "search.json")
        urls = read_urls_file(ws.config_dir / "search_urls.txt")
        index = store.dedup_index()
        if only_sources:  # e.g. ["indeed", "naukri"]: skip LinkedIn, keep only the named extra sources
            cfg = {**cfg, "sources": {k: v for k, v in (cfg.get("sources") or {}).items() if k in only_sources}}
        try:
            if only_sources and "linkedin" not in only_sources:
                raise _SkipLinkedIn
            result = discover(urls, raw_items, search_config=cfg, token=apify_token, index=index,
                              snapshot_dir=ws.logs_dir, dry_run=dry_run, run_id=run_id,
                              search_hits_override=search_hits)
            stats["discovery"] = result["stats"]
            for job in result["eligible"]:
                upsert(job["job_id"], discovery_row(job, "discovered"))
            for job in result["buckets"]["expired"]:
                upsert(job["job_id"], discovery_row(job, "expired"))
            save()
        except _SkipLinkedIn:
            pass
        except ScrapeError as exc:
            log.error("[%s] discovery failed: %s", run_id, exc)
            stats["errors"].append({"stage": "discovery", "error": str(exc)})
        other_fresh = discover_sources(cfg, apify_token, index, ws, run_id, stats, upsert, dry_run, source_items)
        save()
    else:
        other_fresh = []

    fresh = (result["eligible"] if stats["discovery"] else []) + other_fresh
    rows = [r for r in store.all() if not job_ids or str(r["Job ID"]) in job_ids]
    if dry_run and stats["discovery"]:  # dry run: rows weren't written, score the fresh jobs directly
        rows += [{**{"Job ID": j["job_id"]}, **discovery_row(j, "discovered")} for j in fresh]

    # 4-5. score; the 8/10 gate is enforced in score_jobs and again before tailoring
    evaluations: dict[str, dict] = {}
    to_score = [job_from_row(r) for r in rows if r.get("Processing Status") in TO_SCORE]
    if to_score:
        details = discovery_details(ws, fresh)
        to_score = [with_extras(j, details) for j in to_score]
        # free screening first: agencies and red flags never cost an LLM call
        kept = []
        for job in to_score:
            verdict = screen(job)
            if verdict:
                status, reason = verdict
                stats[status] += 1
                log.info("[%s] job_id=%s ref_id=%s %s: %s", run_id, job["job_id"], job.get("ref_id"), status, reason)
                upsert(job["job_id"], {"Processing Status": status, "Error Details": reason})
            else:
                kept.append(job)
        to_score = kept
        save()
        missing = [j["job_id"] for j in to_score if not j.get("seniority_level") and not j.get("experience_required")]
        if missing:
            log.warning("[%s] no seniority/experience metadata for %d jobs; experience cap relies on the "
                        "description only", run_id, len(missing))
    if to_score:
        try:
            scored = score_jobs(to_score, client_factory(score_calls), base.raw, run_id, workers=score_workers)
        except AuthError as exc:
            stats["errors"].append({"stage": "scoring", "error": str(exc)})
            scored = None
        if scored:
            for job in scored["qualified"] + scored["rejected"]:
                ev = job["evaluation"]
                evaluations[job["job_id"]] = ev
                status = ev["status"]
                if ev["match_score"] > TOP_MATCH_THRESHOLD:
                    status = "top_match_unverified"
                    stats["top_matches_new"] += 1
                values = {"Match Score": ev["match_score"], "Matched Skills": ev["matched_skills"],
                          "Missing Skills": ev["missing_skills"], "Preferred Skills": ev.get("preferred_skills") or [],
                          "Processing Status": status, "Error Details": None}
                if ev.get("required_skills"):  # keep the source's list when the model found none
                    values["Required Skills"] = ev["required_skills"]
                upsert(job["job_id"], values)
            for job in scored["unscored"]:
                upsert(job["job_id"], {"Processing Status": "unscored",
                                       "Error Details": "LLM budget/credits/quota reached; will retry next run"})
            for err in scored["errors"]:
                upsert(err["job"]["job_id"], {"Processing Status": "scoring_error", "Error Details": err["error"]})
            for key in ("evaluated", "qualified", "rejected_low_fit", "unscored"):
                stats[key] = scored["stats"][key]
            stats["scoring_errors"] = scored["stats"]["scoring_errors"]
            if not dry_run:
                (ws.logs_dir / f"{run_id}_scoring.json").write_text(json.dumps(
                    {k: scored[k] for k in ("stats", "qualified", "rejected", "errors")}, indent=1,
                    ensure_ascii=False, default=str), encoding="utf-8")
            save()

    # verification: every top match (new or old) is re-checked as open on LinkedIn
    rows = [store.get(str(r["Job ID"])) or r for r in rows]
    if dry_run:
        rows = [{**r, "Processing Status": "top_match_unverified"}
                if evaluations.get(str(r["Job ID"]), {}).get("match_score", 0) > TOP_MATCH_THRESHOLD else r
                for r in rows]
    to_verify = [r for r in rows if r.get("Processing Status") in TO_VERIFY]
    for r in [r for r in to_verify if source_of(r.get("Job URL")) != "LinkedIn"]:
        # Indeed/Naukri: found in a last-24h search and listed as open (expired ones never get here)
        if r.get("Processing Status") != "verified_top_match":
            stats["verified_open"] += 1
        upsert(str(r["Job ID"]), {"Processing Status": "verified_top_match",
                                  "Error Details": "open per the source listing (posted in the last 24 h)"})
    save()
    to_verify = [r for r in to_verify if source_of(r.get("Job URL")) == "LinkedIn"]
    if to_verify:
        no_ref = [r for r in to_verify if not r.get("Source Reference ID")]
        for r in no_ref:
            upsert(str(r["Job ID"]), {"Processing Status": "top_match_unverified",
                                      "Error Details": "no LinkedIn job ID; open status can't be checked"})
        ids = [str(r["Source Reference ID"]) for r in to_verify if r.get("Source Reference ID")]
        states = {}
        if ids and open_checker:
            try:
                states = open_checker(ids)
            except ScrapeError as exc:
                log.error("[%s] open-check failed: %s", run_id, exc)
                stats["errors"].append({"stage": "verification", "error": str(exc)})
        elif ids:
            log.warning("[%s] no Apify token; %d top matches left unverified", run_id, len(ids))
        for r in to_verify:
            ref = str(r.get("Source Reference ID") or "")
            if not ref or ref not in states:
                continue
            is_open, reason = states[ref]
            if is_open:
                stats["verified_open"] += 1
                upsert(str(r["Job ID"]), {"Processing Status": "verified_top_match", "Error Details": None})
            elif is_open is False:
                stats["closed"] += 1
                upsert(str(r["Job ID"]), {"Processing Status": "closed", "Error Details": reason})
            else:
                stats["open_unconfirmed"] += 1
                upsert(str(r["Job ID"]), {"Processing Status": "top_match_unverified", "Error Details": reason})
        save()

    # tailoring (paused in this phase unless --tailor)
    rows = [store.get(str(r["Job ID"])) or r for r in rows]
    if dry_run:
        rows = [{**r, "Match Score": evaluations.get(str(r["Job ID"]), {}).get("fit_score", r.get("Match Score")),
                 "Processing Status": evaluations.get(str(r["Job ID"]), {}).get("status", r.get("Processing Status"))}
                for r in rows]
    to_tailor = [r for r in rows if _needs_tailoring(r, force)] if tailor else []
    if to_tailor:
        client = client_factory(tailor_calls)
        for i, row in enumerate(to_tailor):
            job = job_from_row(row)
            if job["job_id"] in evaluations:
                job["evaluation"] = {**job.get("evaluation", {}), **evaluations[job["job_id"]]}
            upsert(job["job_id"], {"Processing Status": "tailoring"})
            save()
            try:
                rec = process_job(job, base, client, ws, run_id, dry_run=dry_run, force=force)
            except DailyLimitReached as exc:
                log.warning("[%s] %s; %d jobs left for the next run", run_id, exc, len(to_tailor) - i)
                for r in to_tailor[i:]:
                    upsert(str(r["Job ID"]), {"Processing Status": "qualified",
                                              "Error Details": "OpenRouter limit reached; will retry next run"})
                stats["tailoring_deferred"] = len(to_tailor) - i
                save()
                break
            except AuthError as exc:
                upsert(job["job_id"], {"Processing Status": "qualified", "Error Details": str(exc)})
                stats["errors"].append({"stage": "tailoring", "error": str(exc)})
                save()
                break
            except Exception as exc:  # keep the row retryable, then surface the failure
                log.exception("[%s] job_id=%s tailoring crashed", run_id, job["job_id"])
                upsert(job["job_id"], {"Processing Status": "tailoring_error",
                                       "Error Details": f"{exc.__class__.__name__}: {exc}"[:500]})
                stats["errors"].append({"stage": "tailoring", "job_id": job["job_id"], "ref_id": job.get("ref_id"),
                                        "error": str(exc)})
                save()
                continue
            if rec["status"] == "ready_for_review":
                stats["resumes_skipped_existing" if rec.get("skipped") else "resumes_generated"] += 1
                upsert(job["job_id"], {"Processing Status": "ready_for_review", "Resume Path": rec["resume_path"],
                                       "Tailoring Report Path": rec["report_path"], "Error Details": None})
            elif rec["status"] == "validation_failed":
                stats["validation_failed"] += 1
                upsert(job["job_id"], {"Processing Status": "validation_failed", "Error Details": rec["error"]})
            elif rec["status"] == "rejected_low_fit":
                upsert(job["job_id"], {"Processing Status": "rejected_low_fit"})
            save()

    stats["rows_after"] = len(store)
    stats["verified_top_matches_total"] = sum(1 for r in store.all()
                                              if r.get("Processing Status") == "verified_top_match")
    if not dry_run:
        (ws.logs_dir / f"{run_id}_summary.json").write_text(json.dumps(stats, indent=1, ensure_ascii=False),
                                                              encoding="utf-8")
    return stats


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-discovery", action="store_true", help="don't call Apify; work on jobs.xlsx only")
    parser.add_argument("--job-id", action="append", default=[], help="limit scoring/tailoring to these job IDs")
    parser.add_argument("--force", action="store_true", help="regenerate resumes that already exist (with --tailor)")
    parser.add_argument("--tailor", action="store_true", help="generate resumes (paused by default in this phase)")
    parser.add_argument("--dry-run", action="store_true", help="call APIs and print, but write no files")
    parser.add_argument("--sources", help="comma list of sources to search this run: linkedin,indeed,naukri "
                                          "(default: all enabled)")
    parser.add_argument("--reuse-search-dataset", metavar="DATASET_ID",
                        help="use an earlier (already paid) Apify search dataset instead of searching again")
    parser.add_argument("--max-search-results", type=int)
    parser.add_argument("--max-detail-fetches", type=int)
    parser.add_argument("--max-score-calls", type=int, help="default: 35 on a :free model, 600 on paid")
    parser.add_argument("--max-llm-usd", type=float, default=DEFAULT_LLM_SPEND_USD,
                        help=f"OpenRouter spend cap per run (default ${DEFAULT_LLM_SPEND_USD:.2f})")
    parser.add_argument("--max-tailor-calls", type=int, default=DEFAULT_TAILOR_CALLS)
    parser.add_argument("--model", default=None, help=f"OpenRouter model (default {DEFAULT_MODEL})")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv(REPO / ".env")
    except ImportError:
        pass
    ws = get_workspace()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if not args.dry_run:
        ws.logs_dir.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(ws.logs_dir / f"{run_id}.log", encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=handlers)

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        log.error("OPENROUTER_API_KEY is not set (put it in .env)")
        return 1
    model = args.model or os.environ.get("OPENROUTER_MODEL") or DEFAULT_MODEL
    search_config = None
    if args.max_search_results is not None or args.max_detail_fetches is not None:
        try:
            ws.setup()  # seed config/ before reading it on a first run
        except FileNotFoundError as exc:
            log.error("%s", exc)
            return 1
        search_config = load_search_config(ws.config_dir / "search.json")
        if args.max_search_results is not None:
            search_config["max_search_results"] = args.max_search_results
        if args.max_detail_fetches is not None:
            search_config["max_detail_fetches"] = args.max_detail_fetches

    search_hits = None
    if args.reuse_search_dataset:
        from scrape_jobs import dataset_items
        search_hits = dataset_items(args.reuse_search_dataset, os.environ.get("APIFY_API_TOKEN") or "")
        log.info("[%s] reusing %d search hits from dataset %s", run_id, len(search_hits), args.reuse_search_dataset)
    try:
        free = is_free(model)
        score_calls = args.max_score_calls or (DEFAULT_SCORE_CALLS_FREE if free else DEFAULT_SCORE_CALLS_PAID)
        stats = run(ws, run_id=run_id,
                    client_factory=lambda budget: OpenRouterClient(api_key, model, call_budget=budget,
                                                                   spend_cap_usd=None if free else args.max_llm_usd),
                    apify_token=os.environ.get("APIFY_API_TOKEN"), search_config=search_config,
                    skip_discovery=args.skip_discovery, job_ids=args.job_id, force=args.force,
                    dry_run=args.dry_run, score_calls=score_calls, score_workers=1 if free else SCORE_WORKERS_PAID,
                    tailor=args.tailor, tailor_calls=args.max_tailor_calls, search_hits=search_hits,
                    only_sources=[x.strip().lower() for x in args.sources.split(",")] if args.sources else None)
    except (WorkbookLockedError, FileNotFoundError) as exc:
        log.error("[%s] %s", run_id, exc)
        return 1
    print(json.dumps(stats, indent=1, ensure_ascii=False))
    print(f"\njobs.xlsx: {ws.jobs_xlsx}")
    print(f"verified top matches (> {TOP_MATCH_THRESHOLD}, open): {stats.get('verified_top_matches_total', 0)}")
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
