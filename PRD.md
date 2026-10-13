# PRD — Job Application Agent (local version)

## 1. What This Is

A job application AI agent that finds relevant job postings, evaluates each one against the candidate's actual resume, and tailors a separate resume for every job that qualifies. Everything runs locally. Job details are tracked in an Excel workbook, and each tailored resume is saved in its own folder.

**Candidate:** Mallikarjuna Rao Potti
**Career level:** Fresher / entry-level / junior
**Base resume:** `JobApplicationAgent/resume.md`. This is the single source of truth, and the pipeline only ever reads it.

### Target roles
- Data Engineer / Junior Data Engineer / Data Engineering Intern / Graduate Data Engineer
- Data Analyst / Junior Data Analyst / BI Analyst / Business Intelligence Analyst
- Analytics Engineer
- AI Engineer / Junior AI Engineer / Generative AI Engineer / Agentic AI Engineer
- Python Developer roles with relevant data or AI responsibilities

### Skills to emphasize, only where the base resume supports them
Python, SQL, Java; Databricks, PySpark, Delta Lake, ETL/ELT; AWS S3; Pandas, Power BI, forecasting; scikit-learn, LLMs, AI agents, FastAPI.

Never invent experience, skills, employers, certifications, project outcomes, or achievements.

## 2. Goal

One complete run should:

1. Discover fresh job postings with the configured discovery mechanism (Apify).
2. Normalize job data into a consistent schema.
3. Assign a stable internal `job_id` and keep the source's own `ref_id` exactly as given.
4. Deduplicate against jobs already recorded in `jobs.xlsx`.
5. Score each new job against the base resume. The same call extracts matched, missing, required and preferred skills.
6. Keep only jobs scoring **8/10 or higher**.
7. Tailor and validate a resume for every qualifying job, with no fabrication.
8. Save `resume.md` and `tailoring_report.md` in `output/resumes/<job_id>/`.
9. Record job details, the match score, output paths, the processing status and any errors in `jobs.xlsx`.
10. Allow safe reruns: no duplicate jobs, no needless overwriting of existing resumes, and failed work retried later.

### 2.1 Job search preferences

- **Experience (strict):** fresher, graduate, intern, trainee, associate, junior, and 0–1-year roles. A role that requires more than 1 year, or that LinkedIn tags as Mid-Senior, Director or Executive, cannot qualify.
- **Salary:** ₹4–8 LPA is preferred when disclosed. An undisclosed salary never counts against a job.
- **Location:** anywhere in India, including remote work and relocation.
- **Work mode:** remote, hybrid, or onsite.
- **Fit threshold:** 8/10 minimum, enforced in code.
- **Application policy:** the pipeline prepares materials for human review. It never submits applications.

## 3. Pipeline Steps

### 3.1 Job discovery

Two Apify actors are chained:

1. **Search:** `kaix/linkedin-jobs-scraper` runs a keyword × location search with the queries in `config/search.json`. It returns LinkedIn job IDs only.
2. **Details:** `codenest/linkedin-job-search-scraper` fetches full details for each **new** ID. IDs already in `jobs.xlsx` are skipped before this step, so their details are never paid for twice.

Every actor call has `maxItems` and `maxTotalChargeUsd` caps. Expired postings are recorded with status `expired` and are not processed further.

#### Normalized job schema

```json
{
  "job_id": "stable-internal-job-id",
  "ref_id": "source-platform-job-reference-or-null",
  "source_record_id": null,
  "title": "...", "company": "...", "link": "https://...",
  "description": "...", "location": "...", "work_mode": "...", "employment_type": "...",
  "experience_required": "...", "salary": null, "posted_date": "YYYY-MM-DD",
  "source": "Apify", "first_seen_at": "ISO-8601", "last_seen_at": "ISO-8601", "job_status": "open"
}
```

#### Identifier rules
- `job_id` is a deterministic hash of the platform plus `ref_id`. When there is no reliable reference, it is a hash of the platform plus the canonical URL. It is never random and always a string.
- `ref_id` is the platform's own job ID, preserved exactly, or `null`. Never fabricate one.
- `source_record_id` is a separate Apify record ID. The current actor doesn't provide one, so it is always `null`.

#### Deduplication rules
- `job_id` is the primary key, and `jobs.xlsx` is the dedup store.
- A rediscovered posting keeps its existing `job_id` and row.
- Distinct postings are never merged just because their title and company match.
- When the same `job_id` turns up with a different company or title, the conflict is flagged and logged, not merged.

### 3.2 Scoring and filtering

There is one LLM call per job, made through OpenRouter. The default model is `nvidia/nemotron-3.5-lightning:free`, called with forced tool calling.

The model rates four things from 0 to 10: skills match, experience fit, project relevance and role alignment. It also returns matched skills, each with verbatim resume evidence, plus missing skills, required skills, preferred skills, the minimum years required, and its reasoning.

**Python decides the score:**
- `fit = round(0.40·skills + 0.25·experience + 0.20·projects + 0.15·role)`, clamped to 1–10.
- A matched skill whose evidence isn't actually in the resume is moved to missing. Soft skills are ignored. The skills score can't be higher than the share of technical requirements matched.
- The required years are the highest value among the model's extraction, the posting's experience field, and the description text. If that's more than 1, or the seniority is Mid-Senior or above, the score is capped at 7.
- `fit_score >= 8` means qualified; anything lower is `rejected_low_fit`.

Phone, email and links are removed from the resume before it's sent to the model.

### 3.3 Resume tailoring

The model returns a tailoring **plan**, not a free-form resume. The plan has a summary, the order of skills, the order of projects, and reworded bullets, each of which cites the `source_id` of the base-resume line it comes from. Code assembles the resume and copies the name, contact line, employer, dates, project technologies, education and certifications verbatim from the base resume.

Hard rules:
- Never invent employers, titles, responsibilities, dates, tools, certifications, metrics, or achievements.
- Never add a missing requirement as a skill.
- Never present a learning project as professional experience, and never upgrade in-progress work to completed.
- Only reorder and reword verified information.

### 3.4 Resume validation

`scripts/validate_resume.py` is shared by the pipeline and the Claude Code hook (`.claude/hooks/resume_hook.py`). It checks:
- that the structure and every required section are present, with no placeholders, truncation or malformed markup;
- that the name, contact line, employers, dates, education, certifications and project technologies match the base resume;
- that every skill comes from the base resume, and that no number appears that isn't in it;
- that no unsupported technical term and none of the job's missing skills appear;
- that there are no claims of years of experience or seniority;
- that `job_id`, `ref_id` and the job URL are present in the job metadata.

If validation fails, the model gets one corrective retry. If that also fails, no resume is saved under `output/resumes/`, the row is marked `validation_failed` with its reasons, the draft goes to `logs/failed_resumes/`, and the job is retried on the next run.

### 3.5 Output

#### 3.5.1 `jobs.xlsx` (sheet `Jobs`)

| Column | Meaning |
|---|---|
| Job ID | Stable internal identifier (primary key) |
| Source Reference ID | Source-provided ID exactly as given, or empty |
| Job Title | Posting title |
| Company | Hiring company |
| Location | Posting location |
| Job Type | Employment type (Full-time, Internship, …) |
| Work Mode | remote / hybrid / onsite, or empty when unknown |
| Salary | As disclosed, or empty |
| Job URL | Canonical posting URL |
| Job Description | Original description (truncated to Excel's cell limit) |
| Required Skills | Skills the posting lists as required |
| Preferred Skills | Skills the posting lists as nice-to-have |
| Matched Skills | Requirements evidenced in the base resume |
| Missing Skills | Requirements not evidenced, which are never added to the resume |
| Match Score | Integer 1–10 computed in code |
| Application Status | **Owned by the user.** Set to "Not Applied" on insert and never overwritten |
| Date Discovered | Date the job was first seen |
| Resume Path | Absolute path to `output/resumes/<job_id>/resume.md` |
| Tailoring Report Path | Absolute path to `tailoring_report.md` |
| Processing Status | `discovered`, `expired`, `unscored`, `scoring_error`, `rejected_low_fit`, `qualified`, `tailoring`, `validation_failed`, `tailoring_error` or `ready_for_review` |
| Last Updated | UTC timestamp of the last change to the row |
| Error Details | Last error for the row, cleared on success |

Rules:
- Existing rows, and any columns the user adds, are preserved. The workbook is updated in place, never recreated.
- Columns are located by header name.
- Saves are atomic (write to a temp file, then replace), and a `jobs.xlsx.bak` copy is kept.
- If the workbook is open in Excel, the run stops with a clear message.

#### 3.5.2 Resume folders

`output/resumes/<job_id>/` holds:
- `resume.md`: the validated tailored resume;
- `tailoring_report.md`: the job identifiers and URL, the match score and reasoning, matched skills, missing skills that were deliberately not added, what changed (summary, skill order, projects included or omitted, and each reworded bullet with before/after and its `source_id`), and the validation result.

Existing resumes are kept on reruns unless `--force` is given. A deleted resume is regenerated.

### 3.6 Run behaviour and traceability

- Each run gets a run ID. Logs go to `logs/<run_id>.log`, and snapshots go to `logs/<run_id>_discovery.json`, `logs/<run_id>_scoring.json` and `logs/<run_id>_summary.json`.
- Log lines carry the run ID and, where relevant, `job_id` and `ref_id`. Secrets are never logged.
- The free OpenRouter tier allows 50 calls a day. Scoring uses at most 35 per run and tailoring at most 10. When the limit is hit, the remaining jobs stay `unscored` or `qualified` and are picked up on the next run.
- If no job scores 8 or higher, the run still finishes cleanly with accurate counts.

## 4. Out of Scope (this version)

- Google Sheets, Google Drive, Google Cloud, the Google Workspace CLI, and any other cloud storage.
- Hosted or scheduled execution (Modal) and Telegram alerts.
- Company research.
- PDF/DOCX export.
- Automatic application submission or recruiter messaging.
- Fabricating qualifications, reference IDs or links.

## 5. Folder Structure

```text
<Desktop>/JobApplicationAgent/          # data workspace (resolved from the home directory at runtime)
├── PRD.md
├── resume.md                           # base resume, read-only to the pipeline
├── jobs.xlsx
├── output/resumes/<job_id>/{resume.md, tailoring_report.md}
├── logs/
└── config/{search.json, search_urls.txt}

<code repo>/
├── scripts/  run_pipeline.py, scrape_jobs.py, score_jobs.py, tailor_job.py, validate_resume.py,
│             resume_parser.py, excel_store.py, workspace.py, openrouter.py
├── tests/
├── .claude/  hooks/resume_hook.py, skills/tailor-resume/SKILL.md, settings.json
└── .env      APIFY_API_TOKEN, OPENROUTER_API_KEY (gitignored)
```

## 6. Acceptance Criteria

- Discovery returns structured records with stable `job_id` values and exactly preserved `ref_id` values (or `null`). It handles malformed records, empty results and API failures.
- Repeated runs create no duplicate rows and keep each job's `job_id`.
- Every evaluated job gets an integer score from 1 to 10. Jobs below 8 are never tailored, and this is enforced in code.
- Every qualifying job gets its own `resume.md` and `tailoring_report.md`, built only from verified resume facts.
- Validation failures prevent a resume from being saved and are recorded in `jobs.xlsx` and the logs. The job is retried later.
- `jobs.xlsx` holds the absolute resume and report paths, and preserves existing rows and the user's Application Status.
- The base resume is never modified.
- No Google or cloud-storage integration is required.
