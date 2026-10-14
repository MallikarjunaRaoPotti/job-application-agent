# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

This is a **local-only** job application preparation agent for one candidate, a fresher looking for data and AI roles in India. It discovers LinkedIn jobs through Apify, scores them against the base resume through OpenRouter, and tailors a resume for every job scoring 8/10 or higher. All results are tracked in `jobs.xlsx`. It never submits applications.

**There are no Google integrations.** Don't add Google Sheets, Drive, GCP, the Workspace CLI, Modal or cloud storage; the user removed them on purpose. `PRD.md` is the spec.

## Current phase: discovery + scoring + verification only

**Resume tailoring is PAUSED.** `run_pipeline.run(tailor=False)` is the default, and only `--tailor` turns it on. Don't create resumes, resume folders or tailoring reports, and **don't edit `PRD.md`**. This phase's goal is to collect 200+ genuine, open jobs with a **Match Score strictly greater than 9.0**. Match Score is now the unrounded weighted score (e.g. 8.6). The 8/10 qualification still uses the rounded `fit_score`.

- **Daily runs:** each run searches the last 24 hours, and Apify spend is capped at about $3/day in `config/search.json`. Senior titles are skipped before details are bought (`SENIOR_TITLE_RE`), and fresher-sounding titles are fetched first.
- **Detail fallback:** the codenest detail actor silently drops about 70% of jobs. `fetch_details_fallback()` fetches those through kaix's job-ID lookup (`kaix_to_detail()` maps the fields).
- **Screening:** `verify_jobs.screen()` runs **before** scoring, so it costs no LLM call. It marks staffing agencies as `excluded_staffing` (by industry, company name, or hiring-for-a-client wording; a bare "for our clients" is not enough). It marks fees, unpaid work, descriptions under 300 characters, or no company page as `excluded_red_flag`.
- **Top matches:** a score above 9.0 gives `top_match_unverified`. Every run, `verify_jobs.check_open()` re-checks all `top_match_unverified` and `verified_top_match` rows through kaix (`jobState`/`closedNotice`). The result is `verified_top_match`, `closed`, or still unverified. Filter on `Processing Status = verified_top_match` for the deliverable.
- **In-session scoring (no OpenRouter credit):**
  1. `python scripts/agent_scoring.py export` writes `logs/agent_batch_N.json`.
  2. Claude reads it and writes `logs/agent_eval_N.json`: one `submit_evaluation` object per job, plus `job_id`. Each `resume_evidence` must be verbatim resume text.
  3. `python scripts/agent_scoring.py import <file>` runs the normal pipeline with `PrecomputedClient`, so every code guard, the 9.0 bar and the open check still apply. `--rescore` replaces existing scores.

  **Automatic fallback:** `scripts/agent_autoscore.py` does export → headless `claude -p` (Read/Write only, `--add-dir` the workspace logs) → lenient import, for up to N batches. `run_daily.cmd` runs it after the pipeline, so the 8 AM run gets scored even without OpenRouter credit. Claude Code is found through `CLAUDE_BIN`, `PATH`, or the newest `~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude.exe`. Watch for "FOR COLLEGE STUDENTS ONLY" certificate programs, Tier-1-only and Master's-only eligibility, and "N+ years" phrasing the regex misses; set `min_years_required` explicitly.
- **Model:** the paid model `nvidia/nemotron-3.5-lightning` is the default. Scoring runs 8 calls in parallel, with up to 600 calls and `--max-llm-usd` (default $1) per run. An HTTP 402 (no credits) leaves jobs `unscored`.
- **Sources:** LinkedIn, plus Indeed and Naukri through `sources.py`, using `valig/indeed-jobs-scraper` and `valig/naukri-jobs-scraper` with native last-24h filters. Each config query becomes one capped actor run, configured in `search.json` → `sources`. There's no detail stage, because search results already carry the description.
  - `job_id` is namespaced per platform: `make_job_id(ref, url, "indeed")`. LinkedIn IDs are unchanged.
  - Naukri search results only carry a short summary, so `normalize_naukri` appends structured facts (role, skills, education, experience). `description_core_chars` drives a 100-character minimum instead of 300.
  - Naukri's `consultant: true` means a staffing agency.
  - The LinkedIn open-check (kaix) only gets LinkedIn rows. Indeed/Naukri top matches count as open from their last-24h listing.
  - `run_pipeline.py --sources indeed,naukri` runs only those sources.
- **Master sheet:** `views.build_views()` runs on every `JobStore.save()` and creates tabs in this order: Dashboard, Master, Jobs. The old LinkedIn/Indeed/Naukri tabs are deleted.
  - Master lists **every** open job with Match Score ≥ 8.0 from all platforms (not just the last 24 h), excluding closed or expired ones. It is sorted newest posting first and has a `Platform` column. Daily runs only fetch last-24h postings, so Master grows over time.
  - Posting times come from the `logs/*_discovery.json` / `*_source_*.json` snapshots, because the Jobs sheet has no posted-date column.
  - The user fills four columns: `Applied?` and the company-level `Company Domain`, `Email Format` and `Contact Emails`. `JobStore._sync_view_edits()` copies only cells changed since the build (compared with hidden `_built:<column>` columns) back to Jobs. Company fields are written to every Jobs row of that company (matched by `company_key()`), and newer jobs from the same company display them too. `Emails in JD` is extracted automatically from the description.
  - **The Jobs sheet is the agent's full reference data; never delete or rewrite its rows.** The user columns are appended at the end of Jobs.
- **Avoid `\b` in regexes edited through shell heredocs**, because it turns into a backspace character. `scrape_jobs`/`verify_jobs` use letter lookarounds instead.

## Code vs data

- **This repo** holds the code: `scripts/`, `tests/`, `.claude/`, `.env`.
- **The data workspace** is `<Desktop>/JobApplicationAgent/`. `workspace.py` finds the Desktop at runtime (here it's the OneDrive-redirected Desktop), and `JOB_AGENT_HOME` overrides it. The workspace contains:
  - `resume.md`: the base resume and source of truth, which code must **never write**;
  - `jobs.xlsx`;
  - `output/resumes/<job_id>/{resume.md, tailoring_report.md}`;
  - `logs/`;
  - `config/search.json` and `config/search_urls.txt`.

  `Workspace.setup()` seeds missing files from the repo's `config/` and `PRD.md`, but never overwrites existing ones.
- The repo's root `resume.md` is the user's untouched original and is gitignored (never pushed). `tests/fixtures/resume.md` is a fictional sample resume with the same structure, used by tests.

## Commands

```bash
pip install -r requirements.txt
python -m pytest -q                                              # all tests (temp workspace, no network)
python -m pytest tests/test_pipeline.py::test_threshold_boundary_8_tailored_7_not -q
python scripts/run_pipeline.py                                   # full run
python scripts/run_pipeline.py --max-search-results 10 --max-detail-fetches 3   # cheap live run
python scripts/run_pipeline.py --skip-discovery [--job-id ID --force]           # work on jobs.xlsx only
python scripts/run_pipeline.py --dry-run                         # APIs called, nothing written
python scripts/validate_resume.py "<workspace>/output/resumes/<job_id>/resume.md"
python -m pyflakes scripts tests .claude/hooks                   # lint
```

`.env` holds `APIFY_API_TOKEN=` and `OPENROUTER_API_KEY=` as `KEY=value` lines. Earlier versions of this file used other formats, which python-dotenv can't parse.

## Architecture

`run_pipeline.run()` is the only orchestrator, and `jobs.xlsx` is both the tracker and the dedup store. A row's `Processing Status` decides what happens to it on the next run:
- `discovered`, `unscored` and `scoring_error` get scored;
- `qualified`, `tailoring`, `validation_failed` and `tailoring_error` with Match Score ≥ 8 get tailored;
- `ready_for_review` with a missing resume file gets regenerated.

The stages are:

1. **`scrape_jobs.discover()`** chains two Apify actors:
   - `kaix/linkedin-jobs-scraper` does the search and returns IDs only, at about $0.00006 per hit;
   - `codenest/linkedin-job-search-scraper` fetches details at about $0.006 per job, and **can't keyword-search**.

   IDs already in `JobStore.dedup_index()` are dropped before the detail fetch. `job_id` is `sha256("linkedin:ref:<id>")[:16]`, or a hash of the canonical URL when there's no ID. `ref_id` is the platform ID, preserved exactly, or `None`.
2. **`score_jobs.score_jobs()`** makes one OpenRouter call per job via forced tool calling, because the free Nemotron model has no JSON mode. **Python computes the score**:
   - weighted sub-scores;
   - a matched skill counts only with verbatim resume evidence;
   - soft skills are ignored;
   - the skills score is capped by the matched/missing ratio;
   - a role needing more than 1 year, or tagged Mid-Senior or above, is capped at 7 (the strict rule).

   `FIT_THRESHOLD = 8` is checked again in `tailor_job` and `run_pipeline`. The model is lenient, so keep these code-side guards.
3. **`tailor_job.process_job()`**: the model returns a *plan*. `resume_parser` gives every base-resume unit an ID (`SK1`, `EXP1.b1`, `PROJ2`), and rewritten bullets must cite a `source_id`. `apply_plan()` builds the markdown and copies fixed facts verbatim. A resume that fails validation gets one retry; after that nothing is written to `output/resumes`, and the draft goes to `logs/failed_resumes/`.
4. **`validate_resume.validate_resume()`** is the single honesty gate, shared with `.claude/hooks/resume_hook.py`. The hook runs PreToolUse on Write (blocking) and PostToolUse on Edit (marking the row `validation_failed`). It rejects:
   - skills outside their base group;
   - numbers that aren't in the base resume;
   - `TECH_TERMS` terms or the job's Missing Skills that the base resume lacks;
   - claims of N years, expert or senior.

   English-word tech terms such as Excel and Go are matched case-sensitively.
5. **`excel_store.JobStore`** locates columns by header name and preserves rows and user-added columns. **Application Status is user-owned**: it's written only on insert. Saves are atomic and keep a `.bak`. `WorkbookLockedError` is raised when the file is open in Excel.

**Watch out:** `openpyxl`'s `ws.cell(..., value=None)` does *not* clear a cell, so assign `cell.value` directly, as `upsert` does.

**OpenRouter budget:** the free tier allows 50 calls a day. Scoring uses up to 35 per run and tailoring up to 10. When a `DailyLimitReached` is raised, the jobs are left retryable, never rejected.

## Tests

LLM, Apify and filesystem effects are all faked:
- `conftest.isolated_workspace` (autouse) points `JOB_AGENT_HOME` at a temp dir;
- fake LLMs implement `call_tool(messages, tool)`, routed by tool name;
- `test_validation.identity_plan()` plus `apply_plan()` produces a known-good resume, which tests mutate to check that each kind of fabrication fails.

Don't claim a live integration works until it has been run against the real API.
