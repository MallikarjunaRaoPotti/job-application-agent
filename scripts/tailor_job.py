"""Resume tailoring for qualified jobs (fit_score >= 8).

The model never writes a free-form resume. It returns a tailoring PLAN (summary, skill order,
project order, reworded bullets that each cite a base-resume source_id). Code builds the resume
from that plan plus verbatim base-resume facts, validates it, and only then writes:
  <workspace>/output/resumes/<job_id>/resume.md
  <workspace>/output/resumes/<job_id>/tailoring_report.md
A resume that fails validation is never written there; its draft goes to logs/failed_resumes/.
Run via run_pipeline.py.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from openrouter import AuthError, DailyLimitReached, OpenRouterClient, OpenRouterError
from resume_parser import BaseResume
from validate_resume import NUMBER_RE, ValidationResult, validate_resume
from workspace import Workspace

RESUME_FILE = "resume.md"
REPORT_FILE = "tailoring_report.md"
FIT_THRESHOLD = 8          # re-enforced here: never tailor below the cutoff
MAX_TAILOR_ATTEMPTS = 2    # model calls per job (initial + one corrective retry)
MAX_DESCRIPTION_CHARS = 8000

log = logging.getLogger("tailor_job")


class TailorError(RuntimeError):
    pass


# ------------------------------------------------------------------ prompt

SYSTEM_PROMPT = """You tailor a resume for ONE job by producing a TAILORING PLAN. You do not write the
resume itself; code assembles it from your plan and the original resume.

ABSOLUTE RULES (a plan that breaks any of these is rejected):
- Use ONLY facts in the candidate resume. Never add tools, skills, employers, titles, dates,
  certifications, metrics, numbers, or achievements.
- Never add a job requirement the candidate lacks. You are given the candidate's missing
  requirements: none of them may appear anywhere in your output.
- Never claim years of experience, seniority, "expert", or production experience the resume
  doesn't state. The candidate is a fresh graduate with a ~4-month internship.
- Never present a project as professional work experience.
- The BankAI project is IN PROGRESS: never describe it (or the candidate's work generally) as
  completed, end-to-end, deployed, or production-grade in the summary or bullets.
- Every bullet you write must cite the source_id of the ONE original bullet it rewrites, keep that
  bullet's meaning, and keep only numbers that appear in that bullet. Keep in-progress wording
  (e.g. "developing", "planned", "explored") — don't upgrade it to "built" or "delivered".

WHAT YOU MAY DO:
- Write a 2-3 sentence professional summary aimed at this role, using only resume facts.
- Reorder skill groups and the skills inside them so the most relevant come first (you may omit
  irrelevant skills, never add any).
- Order projects by relevance; include at least 3 projects.
- Lightly reword bullets to surface relevant, TRUTHFUL keywords that already appear in the resume.

Call submit_tailoring_plan."""

PLAN_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_tailoring_plan",
        "description": "Submit the resume tailoring plan.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "2-3 sentences, resume facts only"},
                "skill_groups": {
                    "type": "array", "description": "ordered most relevant first",
                    "items": {"type": "object", "properties": {
                        "group_id": {"type": "string"},
                        "items": {"type": "array", "items": {"type": "string"}}},
                        "required": ["group_id", "items"]}},
                "experience": {
                    "type": "array",
                    "items": {"type": "object", "properties": {
                        "entry_id": {"type": "string"},
                        "bullets": {"type": "array", "items": {"type": "object", "properties": {
                            "source_id": {"type": "string"}, "text": {"type": "string"}},
                            "required": ["source_id", "text"]}}},
                        "required": ["entry_id", "bullets"]}},
                "projects": {
                    "type": "array", "description": "ordered most relevant first, at least 3",
                    "items": {"type": "object", "properties": {
                        "entry_id": {"type": "string"},
                        "bullets": {"type": "array", "items": {"type": "object", "properties": {
                            "source_id": {"type": "string"}, "text": {"type": "string"}},
                            "required": ["source_id", "text"]}}},
                        "required": ["entry_id", "bullets"]}},
                "tailoring_notes": {"type": "string", "description": "one sentence on what you emphasized"},
            },
            "required": ["summary", "skill_groups", "experience", "projects"],
        },
    },
}


def build_user_prompt(job: dict, base: BaseResume) -> str:
    ev = job.get("evaluation", {})
    return "\n".join([
        "# CANDIDATE RESUME (IDs in front of each editable unit)", "", base.for_prompt(), "",
        "# TARGET JOB",
        f"Title: {job.get('title')}", f"Company: {job.get('company')}", f"Location: {job.get('location')}", "",
        "Requirements the candidate MATCHES (emphasize these): " + ", ".join(ev.get("matched_skills") or []),
        "Requirements the candidate LACKS (must NOT appear anywhere): " + ", ".join(ev.get("missing_skills") or []),
        "", "## Job description", (job.get("description") or "")[:MAX_DESCRIPTION_CHARS],
    ])


# ------------------------------------------------------------------ plan -> markdown

def _bullets_for(entry, plan_bullets: list, issues: list[str], keep_all: bool) -> list[str]:
    """Map plan bullets onto an entry's base bullets; enforce source_ids and per-bullet numbers."""
    by_id = {b.id: b for b in entry.bullets}
    out, used = [], set()
    for pb in plan_bullets or []:
        sid = str(pb.get("source_id", "")).strip() if isinstance(pb, dict) else ""
        text = str(pb.get("text", "")).strip() if isinstance(pb, dict) else ""
        if sid not in by_id:
            issues.append(f"bullet cites unknown source_id {sid!r} in {entry.id}")
            continue
        if sid in used or not text:
            continue
        extra = set(NUMBER_RE.findall(text)) - set(NUMBER_RE.findall(by_id[sid].text))
        if extra:
            issues.append(f"{sid} introduces numbers not in its source bullet: {sorted(extra)}")
            continue
        used.add(sid)
        out.append(re.sub(r"\s+", " ", text))
    if keep_all or not out:
        out += [b.text for b in entry.bullets if b.id not in used]
    return out[:len(entry.bullets)]


_UNICODE_FIXES = str.maketrans({"‑": "-", "‐": "-", " ": " ", " ": " ", "​": ""})


def _clean_plan(obj):
    """Normalize non-breaking hyphens/spaces that ATS parsers mangle."""
    if isinstance(obj, str):
        return obj.translate(_UNICODE_FIXES)
    if isinstance(obj, list):
        return [_clean_plan(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _clean_plan(v) for k, v in obj.items()}
    return obj


def apply_plan(base: BaseResume, plan: dict) -> tuple[str, list[str]]:
    """Build markdown from a plan. Returns (markdown, fatal_issues)."""
    issues: list[str] = []
    plan = _clean_plan(plan)
    summary = re.sub(r"\s+", " ", str(plan.get("summary") or "")).strip()
    if len(summary) < 80:
        issues.append("summary missing or too short")

    # skills: known groups only, items restricted to the group's base items (base casing)
    groups_out, seen_groups = [], set()
    for g in plan.get("skill_groups") or []:
        group = base.skill_group(str(g.get("group_id", "")).strip()) if isinstance(g, dict) else None
        if not group or group.id in seen_groups:
            continue
        seen_groups.add(group.id)
        lookup = {i.lower(): i for i in group.items}
        items = list(dict.fromkeys(lookup[i.strip().lower()] for i in g.get("items") or []
                                   if isinstance(i, str) and i.strip().lower() in lookup))
        groups_out.append((group.name, items or group.items))
    groups_out += [(g.name, g.items) for g in base.skills if g.id not in seen_groups]

    # experience: every base entry, every bullet (missing ones fall back to the original)
    plan_exp = {str(e.get("entry_id")): e.get("bullets") for e in plan.get("experience") or [] if isinstance(e, dict)}
    exp_out = [(e, _bullets_for(e, plan_exp.get(e.id), issues, keep_all=True)) for e in base.experience]

    # projects: plan order, known ids only, at least 3
    proj_out, seen = [], set()
    for p in plan.get("projects") or []:
        entry = base.entry(str(p.get("entry_id", ""))) if isinstance(p, dict) else None
        if not entry or not entry.id.startswith("PROJ"):
            issues.append(f"unknown project id {p.get('entry_id') if isinstance(p, dict) else p!r}")
            continue
        if entry.id in seen:
            continue
        seen.add(entry.id)
        proj_out.append((entry, _bullets_for(entry, p.get("bullets"), issues, keep_all=False)))
    for entry in base.projects:
        if len(proj_out) >= min(3, len(base.projects)):
            break
        if entry.id not in seen:
            proj_out.append((entry, [b.text for b in entry.bullets]))

    lines = [f"# {base.name}", "", base.contact, "", "## Professional Summary", "", summary, "",
             "## Technical Skills", ""]
    lines += [f"- **{name}:** {', '.join(items)}" for name, items in groups_out]
    lines += ["", "## Experience", ""]
    for entry, bullets in exp_out:
        lines += [f"### {entry.heading}", entry.meta, ""] + [f"- {b}" for b in bullets] + [""]
    lines += ["## Selected Projects", ""]
    for entry, bullets in proj_out:
        lines += [f"### {entry.heading}", entry.meta, ""] + [f"- {b}" for b in bullets] + [""]
    lines += ["## Education", "", base.education_md, "", "## Certifications & Achievement", ""]
    lines += [f"- {c}" for c in base.certifications]
    return "\n".join(lines) + "\n", issues


# ------------------------------------------------------------------ per job

def tailor_one(job: dict, base: BaseResume, client: OpenRouterClient) -> tuple[str, dict, ValidationResult, int]:
    """Return (markdown, plan, validation, attempts). Raises TailorError if every attempt fails."""
    meta = {"job_id": job["job_id"], "ref_id": job.get("ref_id"), "job_url": job.get("link")}
    gaps = job.get("evaluation", {}).get("missing_skills") or []
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(job, base)}]
    last_md, last_errors = "", []
    for attempt in range(1, MAX_TAILOR_ATTEMPTS + 1):
        try:
            plan = client.call_tool(messages, PLAN_TOOL, temperature=0.2)
        except (AuthError, DailyLimitReached):
            raise
        except OpenRouterError as exc:
            last_errors = [str(exc)]
            messages.append({"role": "user", "content": f"Invalid response ({exc}). Call submit_tailoring_plan."})
            continue
        md, issues = apply_plan(base, plan)
        result = validate_resume(md, base, meta, gaps)
        for issue in issues:
            result.fail(issue)
        last_md, last_errors = md, result.errors
        if result.passed:
            return md, plan, result, attempt
        log.warning("job_id=%s ref_id=%s attempt %d failed validation: %s", job["job_id"], job.get("ref_id"),
                    attempt, "; ".join(result.errors[:5]))
        messages.append({"role": "user", "content": "Your plan was rejected for these reasons:\n- "
                         + "\n- ".join(result.errors[:15]) + "\nFix every issue and call submit_tailoring_plan again."})
    err = TailorError("; ".join(last_errors[:10]) or "no valid plan")
    err.markdown, err.errors = last_md, last_errors
    raise err


def tailoring_report(job: dict, base: BaseResume, plan: dict, result: ValidationResult, attempts: int,
                     model: str, resume_path: Path) -> str:
    """Human-readable explanation of what was changed and why, plus validation results."""
    plan = _clean_plan(plan)
    ev = job.get("evaluation", {})
    included = [str(p.get("entry_id")) for p in plan.get("projects") or [] if isinstance(p, dict)]
    included = [i for i in dict.fromkeys(included) if base.entry(i)]
    omitted = [p.heading for p in base.projects if p.id not in included]
    group_order = [base.skill_group(str(g.get("group_id"))) for g in plan.get("skill_groups") or [] if isinstance(g, dict)]
    reworded = []
    for section in (plan.get("experience") or []) + (plan.get("projects") or []):
        for b in (section.get("bullets") or []) if isinstance(section, dict) else []:
            src = base.bullet(str(b.get("source_id", ""))) if isinstance(b, dict) else None
            if src and str(b.get("text", "")).strip() and str(b["text"]).strip() != src.text:
                reworded.append((src.id, src.text, str(b["text"]).strip()))

    lines = [
        f"# Tailoring report: {job.get('title')} at {job.get('company')}", "",
        f"- **Job ID:** {job['job_id']}",
        f"- **Source Reference ID:** {job.get('ref_id') or 'none provided by source'}",
        f"- **Job URL:** {job.get('link')}",
        f"- **Match Score:** {ev.get('fit_score')}/10",
        f"- **Resume:** {resume_path}",
        f"- **Model:** {model} ({attempts} attempt{'s' if attempts != 1 else ''})",
        f"- **Generated:** {datetime.now(timezone.utc).replace(microsecond=0).isoformat()}", "",
        "## Why this job qualified", "", ev.get("reasoning") or "(no reasoning recorded)", "",
        "## Matched skills (emphasized)", "", ", ".join(ev.get("matched_skills") or []) or "(none recorded)", "",
        "## Missing skills (deliberately NOT added to the resume)", "",
        ", ".join(ev.get("missing_skills") or []) or "(none recorded)", "",
        "## What changed", "",
        f"- **Summary** rewritten for the role. Notes: {plan.get('tailoring_notes') or 'n/a'}",
        "- **Skill groups order:** " + (", ".join(g.name for g in group_order if g) + " first, others in base order"
                                        if any(group_order) else "unchanged (base order)"),
        "- **Projects included, in order:** " + ", ".join(base.entry(i).heading for i in included),
        "- **Projects omitted:** " + (", ".join(omitted) or "none"),
        "- Name, contact, employer, dates, project technologies, education and certifications are copied "
        "verbatim from the base resume.", "",
        "### Reworded bullets", "",
    ]
    if reworded:
        for sid, old, new in reworded:
            lines += [f"- `{sid}`", f"  - before: {old}", f"  - after: {new}"]
    else:
        lines.append("- none (original wording kept)")
    lines += ["", "## Validation", "", f"- **Result:** {'PASSED' if result.passed else 'FAILED'}",
              "- Checks: structure, placeholders, verbatim facts, skills subset, numbers, unsupported "
              "technical terms, missing-skill terms, experience-length and seniority claims, job metadata."]
    lines += [f"- Warning: {w}" for w in result.warnings]
    lines += ["", "_Review before applying. Applications are never submitted automatically._", ""]
    return "\n".join(lines)


def process_job(job: dict, base: BaseResume, client: OpenRouterClient, ws: Workspace, run_id: str,
                *, dry_run: bool = False, force: bool = False) -> dict:
    """Tailor, validate and save one job. Returns a record for the run log and jobs.xlsx."""
    jid, ref = job["job_id"], job.get("ref_id")
    rec = {"job_id": jid, "ref_id": ref}
    fit = job.get("evaluation", {}).get("fit_score")
    if not isinstance(fit, int) or fit < FIT_THRESHOLD:
        log.error("[%s] job_id=%s ref_id=%s fit_score=%s below %d; refusing to tailor", run_id, jid, ref, fit, FIT_THRESHOLD)
        return {**rec, "status": "rejected_low_fit"}

    folder = ws.job_dir(jid)
    resume_path, report_path = folder / RESUME_FILE, folder / REPORT_FILE
    if resume_path.is_file() and report_path.is_file() and not force:
        log.info("[%s] job_id=%s already has a resume at %s; skipping (use --force)", run_id, jid, resume_path)
        return {**rec, "status": "ready_for_review", "resume_path": str(resume_path),
                "report_path": str(report_path), "skipped": True}

    try:
        md, plan, result, attempts = tailor_one(job, base, client)
    except TailorError as exc:
        log.error("[%s] job_id=%s ref_id=%s validation_failed: %s", run_id, jid, ref, exc)
        if not dry_run:
            failed_dir = ws.logs_dir / "failed_resumes"
            failed_dir.mkdir(parents=True, exist_ok=True)
            (failed_dir / f"{jid}.md").write_text(getattr(exc, "markdown", "") or "", encoding="utf-8")
        return {**rec, "status": "validation_failed", "error": "; ".join(getattr(exc, "errors", [str(exc)])[:10])}

    if dry_run:
        print(md)
        return {**rec, "status": "dry_run_passed"}

    folder.mkdir(parents=True, exist_ok=True)
    for path, content in ((resume_path, md),
                          (report_path, tailoring_report(job, base, plan, result, attempts, client.model, resume_path))):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    log.info("[%s] job_id=%s ref_id=%s resume validated -> %s", run_id, jid, ref, resume_path)
    return {**rec, "status": "ready_for_review", "resume_path": str(resume_path.resolve()),
            "report_path": str(report_path.resolve())}
