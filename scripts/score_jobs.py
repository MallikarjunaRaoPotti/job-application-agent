"""Score discovered jobs against the base resume (workspace resume.md) and enforce the 8/10 cutoff.

One OpenRouter call per job returns rubric sub-scores plus matched/missing requirements
(via tool calling, since the free Nemotron model has no JSON mode). Python then:
  - drops "matched" skills whose resume evidence isn't actually in the resume,
  - combines sub-scores with fixed weights,
  - caps any job requiring more than 1 year of experience below the cutoff,
  - applies fit_score >= 8 -> qualified.
The model never decides qualification. The same call also extracts the posting's required and
preferred skills for the jobs.xlsx tracker. Run via run_pipeline.py.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from openrouter import (AuthError, DailyLimitReached, OpenRouterClient, OpenRouterError,
                        parse_tool_response as _parse_tool_response)



# Hard rules, enforced here in code (not configurable).
FIT_THRESHOLD = 8
MAX_YEARS_ALLOWED = 1          # strict: roles requiring > 1 year can't qualify
EXPERIENCE_CAP_SCORE = 7       # score ceiling for such roles
WEIGHTS = {"skills_match": 0.40, "experience_fit": 0.25, "project_relevance": 0.20, "role_alignment": 0.15}

MAX_DESCRIPTION_CHARS = 12000
DEFAULT_CALL_BUDGET = 35         # of the 50/day free quota; the rest is left for tailoring + research
SENIOR_LEVELS = {"mid-senior level", "director", "executive"}  # LinkedIn seniority labels capped like 2+ yrs
SOFT_SKILL_RE = re.compile(r"communicat|collaborat|team|problem[- ]solving|analytical|leadership|"
                           r"attention to detail|interpersonal|self[- ]motivated|eager to learn|willingness to learn|"
                           r"quick learner|learning agility|passion", re.I)   # NOT bare "learn": machine learning is technical

# "2+ years of experience", "1 to 3 years relevant experience", "experience: 3-5 yrs"
_YEARS = r"(\d+(?:\.\d+)?)\s*\+?\s*(?:(?:-|–|to)\s*\d+(?:\.\d+)?\s*)?(?:years?|yrs?)"
DESC_YEARS_RES = [re.compile(_YEARS + r"[^.\n]{0,40}?experience", re.I),
                  re.compile(r"experience[^.\n]{0,30}?" + _YEARS, re.I)]

log = logging.getLogger("score_jobs")


ScoringError = OpenRouterError  # a single job could not be scored


# ------------------------------------------------------------------ resume

def redact_contact(resume: str) -> str:
    """Strip phone, email and links before sending the resume to a third-party model."""
    text = re.sub(r"\[([^\]]*)\]\((?:https?://|mailto:)[^)]*\)", "", resume)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "", text)
    text = re.sub(r"\+?\d[\d\s-]{8,}\d", "", text)
    lines = [ln for ln in text.splitlines() if re.sub(r"[|\s]", "", ln) or not ln.strip()]
    return "\n".join(lines)


def _norm(text: str) -> str:
    text = re.sub(r"[*_`#>]", "", text.lower())
    text = text.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", text).strip()


def evidence_in_resume(evidence: str | None, resume_norm: str) -> bool:
    ev = _norm(evidence or "")
    return len(ev) >= 2 and ev in resume_norm


# ------------------------------------------------------------------ prompt

SYSTEM_PROMPT = """You evaluate how well ONE candidate fits ONE job posting. Be strict and evidence-based.

The candidate's resume is the only source of truth. A skill counts as matched only if the resume
shows it. Absent evidence means unverified, not "probably has it". Never match on job title or
keyword similarity alone.

Candidate facts to apply:
- Total professional experience: about 4 months (one engineering internship, Jun 2026 - Oct 2026).
  Treat the candidate as a fresher / 0-1 year profile. Projects are projects, not work experience.
- B.Tech CSIT graduate, Apr 2026.
- Any resume line describing exam "preparation" or a conditional note is NOT an earned certification.

Rate each dimension 0-10:
- skills_match: share of the job's CORE requirements evidenced in the resume. Optional/nice-to-have
  items should not drag this down much.
- experience_fit: 10 = fresher/intern/graduate/0-1 yr role; 7 = unstated but clearly junior;
  4 = unstated and reads mid-level; 0-2 = clearly needs 2+ years or senior/lead.
- project_relevance: do the resume's projects demonstrate the kind of work this job does?
- role_alignment: 10 = data engineering, data/BI analytics, analytics engineering, AI/GenAI/agentic AI
  engineering, or Python roles with data/AI work; low for sales, support, unrelated engineering, etc.

Also extract min_years_required: the minimum years of experience the posting REQUIRES, as a number
(e.g. "2-4 years" -> 2, "fresher" -> 0, "1+ years" -> 1). Use null if the posting does not state it.

For every matched skill, give resume_evidence: a SHORT phrase copied VERBATIM from the resume that
proves it (e.g. "PySpark, Databricks, Delta Lake"). Matches without verbatim evidence are discarded.

Also list the posting's required_skills and preferred_skills (nice-to-have) as short names, exactly
as the posting states them; leave a list empty if the posting doesn't say.

Location: anywhere in India, including remote or relocation, is a match. Salary: undisclosed salary
is neutral, never a negative. Call the submit_evaluation tool with your result."""

EVAL_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_evaluation",
        "description": "Submit the structured fit evaluation for this job.",
        "parameters": {
            "type": "object",
            "properties": {
                "skills_match": {"type": "integer", "minimum": 0, "maximum": 10},
                "experience_fit": {"type": "integer", "minimum": 0, "maximum": 10},
                "project_relevance": {"type": "integer", "minimum": 0, "maximum": 10},
                "role_alignment": {"type": "integer", "minimum": 0, "maximum": 10},
                "min_years_required": {"type": ["number", "null"]},
                "matched_skills": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"skill": {"type": "string"}, "resume_evidence": {"type": "string"}},
                        "required": ["skill", "resume_evidence"],
                    },
                },
                "missing_skills": {"type": "array", "items": {"type": "string"}},
                "required_skills": {"type": "array", "items": {"type": "string"},
                                    "description": "skills/tools the posting lists as required, short names"},
                "preferred_skills": {"type": "array", "items": {"type": "string"},
                                     "description": "skills/tools the posting lists as preferred/nice-to-have"},
                "experience_match": {"type": "string", "description": "one sentence"},
                "project_match": {"type": "string", "description": "one sentence"},
                "location_match": {"type": "string", "description": "one sentence"},
                "salary_match": {"type": "string", "description": "one sentence"},
                "reasoning": {"type": "string", "description": "2-4 sentences"},
            },
            "required": ["skills_match", "experience_fit", "project_relevance", "role_alignment",
                         "min_years_required", "matched_skills", "missing_skills", "experience_match",
                         "project_match", "location_match", "salary_match", "reasoning"],
        },
    },
}


def build_user_prompt(job: dict, resume: str) -> str:
    desc = (job.get("description") or "")[:MAX_DESCRIPTION_CHARS]
    fields = {k: job.get(k) for k in ("title", "company", "location", "work_mode", "employment_type",
                                      "experience_required", "seniority_level", "salary")}
    return (f"# CANDIDATE RESUME\n\n{resume}\n\n# JOB POSTING\n\n"
            f"{json.dumps(fields, ensure_ascii=False, indent=1)}\n\n## Description\n\n{desc or '(no description provided)'}")


# ------------------------------------------------------------------ LLM call

def parse_tool_response(data: dict) -> dict:
    return _parse_tool_response(data, "submit_evaluation")


# ------------------------------------------------------------------ scoring logic

def _subscore(raw: dict, key: str) -> int:
    val = raw.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ScoringError(f"missing or non-numeric {key}")
    return max(0, min(10, int(round(val))))


def _years_from_text(text: str | None) -> float | None:
    """Minimum years from a structured experience field like '2-4 years' or '3+ yrs'."""
    if not text:
        return None
    if re.search(r"\b(fresher|no experience|entry[- ]level|0\s*(-|to)\s*\d)", text, re.I):
        return 0.0
    m = re.search(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:(?:-|to)\s*\d+(?:\.\d+)?\s*)?(?:years?|yrs?)", text, re.I)
    return float(m.group(1)) if m else None


def years_from_description(desc: str | None) -> float | None:
    """Smallest 'N years ... experience' requirement stated in the description, if any."""
    found = [float(m.group(1)) for rx in DESC_YEARS_RES for m in rx.finditer(desc or "")]
    found = [y for y in found if y <= 20]
    return min(found) if found else None


def _str_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(str(v).strip() for v in value if isinstance(v, (str, int, float)) and str(v).strip()))


def compute_evaluation(job: dict, raw: dict, resume_norm: str) -> dict:
    """Turn the model's raw output into the final, code-decided evaluation."""
    subs = {k: _subscore(raw, k) for k in WEIGHTS}

    matched, unverified = [], []
    for item in raw.get("matched_skills") or []:
        if isinstance(item, dict) and item.get("skill"):
            skill = str(item["skill"]).strip()
            if SOFT_SKILL_RE.search(skill):
                continue  # soft skills don't count as evidence of technical fit
            (matched if evidence_in_resume(item.get("resume_evidence"), resume_norm) else unverified).append(skill)
        elif isinstance(item, str) and item.strip():
            unverified.append(item.strip())
    missing = [str(s).strip() for s in raw.get("missing_skills") or [] if str(s).strip()]
    missing += [s for s in unverified if s not in missing]
    claimed = len(matched) + len(unverified)
    if unverified and claimed:
        subs["skills_match"] = int(round(subs["skills_match"] * len(matched) / claimed))
    # Consistency check: skills score can't exceed the share of technical requirements matched.
    tech_missing = [m for m in missing if not SOFT_SKILL_RE.search(m)]
    if matched or tech_missing:
        ratio_cap = math.ceil(10 * len(matched) / (len(matched) + len(tech_missing)))
        subs["skills_match"] = min(subs["skills_match"], ratio_cap)

    weighted = sum(subs[k] * w for k, w in WEIGHTS.items())
    match_score = max(1.0, min(10.0, weighted))          # unrounded; used for the > 9.0 top-match bar
    fit_score = max(1, min(10, math.floor(weighted + 0.5)))

    # Strict: take the highest requirement any source states.
    model_years = raw.get("min_years_required")
    candidates = {
        "model": None if isinstance(model_years, bool) or not isinstance(model_years, (int, float)) else float(model_years),
        "experience_required": _years_from_text(job.get("experience_required")),
        "description": years_from_description(job.get("description")),
    }
    stated = {k: v for k, v in candidates.items() if v is not None}
    years_source = max(stated, key=stated.get) if stated else None
    years = stated[years_source] if stated else None

    rejection_reasons = []
    if years is not None and years > MAX_YEARS_ALLOWED:
        fit_score = min(fit_score, EXPERIENCE_CAP_SCORE)
        match_score = min(match_score, float(EXPERIENCE_CAP_SCORE))
        rejection_reasons.append(f"requires {years:g}+ years of experience (max allowed {MAX_YEARS_ALLOWED})")
    seniority = (job.get("seniority_level") or "").strip().lower()
    if seniority in SENIOR_LEVELS:
        fit_score = min(fit_score, EXPERIENCE_CAP_SCORE)
        match_score = min(match_score, float(EXPERIENCE_CAP_SCORE))
        rejection_reasons.append(f"LinkedIn seniority '{job.get('seniority_level')}' is above fresher level")
    qualified = fit_score >= FIT_THRESHOLD
    if not qualified:
        rejection_reasons.append(f"fit_score {fit_score} below {FIT_THRESHOLD}")
        if subs["role_alignment"] <= 4:
            rejection_reasons.append("role outside target families")
        if subs["skills_match"] <= 5:
            rejection_reasons.append("core skills not evidenced in resume")
    if unverified:
        log.info("job_id=%s dropped %d unverified matched skills: %s", job.get("job_id"), len(unverified), unverified)

    return {
        "job_id": job["job_id"],
        "ref_id": job.get("ref_id"),
        "fit_score": fit_score,
        "match_score": round(match_score, 2),
        "weighted_score": round(weighted, 2),
        "sub_scores": subs,
        "min_years_required": years,
        "min_years_source": years_source,
        "matched_skills": matched,
        "missing_skills": missing,
        "unverified_claims": unverified,
        "experience_match": raw.get("experience_match"),
        "project_match": raw.get("project_match"),
        "location_match": raw.get("location_match"),
        "salary_match": raw.get("salary_match"),
        "reasoning": raw.get("reasoning"),
        "required_skills": _str_list(raw.get("required_skills")),
        "preferred_skills": _str_list(raw.get("preferred_skills")),
        "qualification_status": "qualified" if qualified else "rejected",
        "status": "qualified" if qualified else "rejected_low_fit",
        "rejection_reasons": [] if qualified else rejection_reasons,
    }


def score_job(job: dict, client: OpenRouterClient, resume_redacted: str, resume_norm: str) -> dict:
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(job, resume_redacted)}]
    if hasattr(client, "evaluation_for"):  # precomputed (agent-written) evaluation; same code guards apply
        return compute_evaluation(job, client.evaluation_for(job), resume_norm)
    try:
        raw = client.call_tool(messages, EVAL_TOOL)
        return compute_evaluation(job, raw, resume_norm)
    except AuthError:
        raise
    except ScoringError as first:
        # One corrective retry for malformed output.
        log.warning("job_id=%s ref_id=%s scoring retry after: %s", job["job_id"], job.get("ref_id"), first)
        messages.append({"role": "user", "content": f"Your previous answer was invalid ({first}). "
                         "Call submit_evaluation with every required field."})
        raw = client.call_tool(messages, EVAL_TOOL)
        return compute_evaluation(job, raw, resume_norm)


# ------------------------------------------------------------------ pipeline

def score_jobs(jobs: list[dict], client: OpenRouterClient, resume: str, run_id: str, workers: int = 1) -> dict:
    """Score jobs (in parallel when workers > 1). A spent budget/quota leaves the rest unscored."""
    resume_redacted, resume_norm = redact_contact(resume), _norm(resume)
    results = {"qualified": [], "rejected": [], "unscored": [], "errors": []}
    stop = threading.Event()
    lock = threading.Lock()

    def one(job: dict) -> None:
        if stop.is_set():
            with lock:
                results["unscored"].append(job)
            return
        try:
            ev = score_job(job, client, resume_redacted, resume_norm)
        except DailyLimitReached as exc:
            if not stop.is_set():
                log.warning("[%s] %s; remaining jobs left unscored for next run", run_id, exc)
            stop.set()
            with lock:
                results["unscored"].append(job)
            return
        except ScoringError as exc:
            if isinstance(exc, AuthError):
                stop.set()
                raise
            log.error("[%s] job_id=%s ref_id=%s scoring failed: %s", run_id, job["job_id"], job.get("ref_id"), exc)
            with lock:
                results["errors"].append({"job": job, "status": "scoring_error", "error": str(exc)})
            return
        bucket = "qualified" if ev["fit_score"] >= FIT_THRESHOLD else "rejected"  # code-enforced gate
        with lock:
            results[bucket].append({**job, "evaluation": ev})
        log.info("[%s] job_id=%s ref_id=%s %s @ %s -> %.2f/10 %s", run_id, job["job_id"], job.get("ref_id"),
                 job.get("title"), job.get("company"), ev["match_score"], ev["status"])

    auth_error = None
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for fut in [pool.submit(one, job) for job in jobs]:
            try:
                fut.result()
            except AuthError as exc:
                auth_error = exc
    if auth_error:
        raise auth_error
    results["stats"] = {"run_id": run_id, "input_jobs": len(jobs),
                        "evaluated": len(results["qualified"]) + len(results["rejected"]),
                        "qualified": len(results["qualified"]), "rejected_low_fit": len(results["rejected"]),
                        "unscored": len(results["unscored"]), "scoring_errors": len(results["errors"])}
    return results
