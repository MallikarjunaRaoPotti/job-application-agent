---
name: tailor-resume
description: Tailor the candidate's base resume for one qualified job (Match Score >= 8) without fabrication, saving resume.md and tailoring_report.md under JobApplicationAgent/output/resumes/<job_id>/ and recording the paths in jobs.xlsx. Use when asked to tailor, rewrite, or prepare a resume for a specific job or job ID.
---

# Tailor resume for one job

Everything is local, under `<Desktop>/JobApplicationAgent/`. The base resume `JobApplicationAgent/resume.md` is the **only** source of truth, and it is never modified. You reorder and reword; you never add.

## Before tailoring

1. Find the job's row in `JobApplicationAgent/jobs.xlsx` (sheet `Jobs`) by **Job ID**. You need its Job URL, Job Description, Match Score, Matched Skills and Missing Skills.
2. **Stop if Match Score < 8 or is empty.** Don't tailor unsuitable jobs, and don't stretch the resume to make them look suitable.
3. Read the base resume in full.

## Preferred path: the pipeline

```bash
python scripts/run_pipeline.py --skip-discovery --job-id <job_id> --dry-run          # preview, writes nothing
python scripts/run_pipeline.py --skip-discovery --job-id <job_id> --force            # (re)generate + update jobs.xlsx
```

## Manual edits

Edit `JobApplicationAgent/output/resumes/<job_id>/resume.md`. The `resume_hook` validates every Write or Edit of that file against the base resume and that job's Missing Skills. It blocks invalid writes and marks the job's row `validation_failed`.

Keep this exact layout:

```
# <Candidate Name>
<contact line copied verbatim>
## Professional Summary
## Technical Skills        - **<base group name>:** <items from that base group only>
## Experience              ### heading + **dates** verbatim; all bullets kept
## Selected Projects       >= 3 base projects; ### name + **Technologies:** line verbatim
## Education               verbatim
## Certifications & Achievement   verbatim
```

### You may
- Write a 2–3 sentence summary for the role that uses only resume facts.
- Reorder skill groups and the items within them, and drop items that aren't relevant.
- Order projects by relevance, keeping at least 3.
- Lightly reword bullets to bring forward truthful keywords that are already in the resume.

### You must never
- Add any tool, skill, employer, title, date, certification, number, or achievement that isn't in the base resume.
- Add anything from the job's Missing Skills, even worded as "familiar with".
- Claim years of experience, seniority, "expert", or production experience. The candidate is a 2026 graduate with about 4 months of internship.
- Present a project as professional work, or upgrade in-progress wording ("developing", "planned", "explored").

## After manual edits

1. `python scripts/validate_resume.py "<path>/resume.md"` must print `"passed": true`.
2. Note what you changed in `tailoring_report.md` next to the resume.
3. Don't submit applications or message recruiters. The output is for human review.
