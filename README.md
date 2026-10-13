# Job Application Agent (local)

This tool finds entry-level data and AI jobs on LinkedIn (through Apify) and scores each one against your resume. For every job that scores **8/10 or higher**, it writes an honest tailored resume. Everything stays on your machine: job details go into an Excel file, and each resume goes into its own folder. Nothing is uploaded to Google or any cloud storage, and no application is ever submitted for you.

## Where things are

```text
<Desktop>/JobApplicationAgent/
├── PRD.md
├── resume.md                      ← your base resume (the pipeline only reads it)
├── jobs.xlsx                      ← job tracker, sheet "Jobs"
├── output/resumes/<job_id>/
│   ├── resume.md                  ← tailored resume
│   └── tailoring_report.md        ← what changed, why, and the validation result
├── logs/                          ← run logs, run summaries, failed resume drafts
└── config/search.json             ← search queries, locations and spend caps
```

The Desktop is found at runtime (an OneDrive-redirected Desktop works too). Set `JOB_AGENT_HOME` to put the workspace somewhere else.

## Setup

```bash
pip install -r requirements.txt
```

Create a `.env` file in this repo with these two lines (see `.env.example`):

```
APIFY_API_TOKEN=...
OPENROUTER_API_KEY=...
```

## Current phase

Resume generation is **paused**. Runs only discover, screen, score and verify jobs, and the goal is 200+ open jobs with Match Score > 9.0. In `jobs.xlsx`, filter **Processing Status = `verified_top_match`**. Excluded jobs (`excluded_staffing`, `excluded_red_flag`, `closed`) show the reason in **Error Details**. Pass `--tailor` only when resume generation is resumed.

## Run

```bash
python scripts/run_pipeline.py                                  # discover -> score -> tailor -> jobs.xlsx
python scripts/run_pipeline.py --skip-discovery                 # only finish work already in jobs.xlsx
python scripts/run_pipeline.py --max-search-results 10 --max-detail-fetches 3   # small, cheap run
python scripts/run_pipeline.py --skip-discovery --job-id <id> --force           # regenerate one resume
python scripts/run_pipeline.py --dry-run                        # call the APIs, but write no files
```

**Daily schedule:** the Windows Task Scheduler task **"JobApplicationAgent Daily"** runs `scripts/run_daily.cmd` every day at 8:00 AM. If the PC was off at 8:00, it runs as soon as you log in. Its output is appended to `JobApplicationAgent/logs/scheduler.log`. You can manage it in Task Scheduler, or run it now with `schtasks /Run /TN "JobApplicationAgent Daily"`.

Close `jobs.xlsx` in Excel before running, because Excel locks the file.

**Reruns are safe:**
- Jobs already in `jobs.xlsx` are not fetched again and don't create duplicate rows.
- Existing resumes are kept unless you pass `--force`.
- Jobs that failed or were deferred are retried.
- Your **Application Status** column is never overwritten.

## Test

```bash
python -m pytest -q
python -m pytest tests/test_pipeline.py::test_rerun_creates_no_duplicates_and_keeps_resumes -q
```

Tests use a temporary workspace and fake Apify and LLM responses, so they never touch your Desktop and cost nothing.

## Costs and limits

- **Apify:** each run has spending caps set in `config/search.json`. A full run costs at most about $0.40.
- **OpenRouter free model:** 50 calls a day. Scoring uses up to 35 per run and tailoring up to 10. Jobs that don't get done that day are picked up the next day.

## Honesty guarantees

The tailoring model only proposes a plan: reordering, plus rewording that cites the original line. Code assembles the resume. `scripts/validate_resume.py` rejects any resume with:
- tools, numbers, projects or certifications that aren't in your base resume;
- any of the job's missing skills;
- claims of years of experience or seniority;
- changed dates or CGPA.

Read each resume and its `tailoring_report.md` before applying.
