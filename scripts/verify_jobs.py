"""Verification: is a job genuine (direct employer, no red flags) and currently open?

- screen(job): free local checks run BEFORE scoring, so excluded jobs never cost an LLM call.
    Mid-Senior+ seniority or > 1 year required (strict rule) -> "excluded_experience"
    staffing/recruitment agencies  -> "excluded_staffing"
    fees, unpaid work, empty/too-short description, no company page -> "excluded_red_flag"
- check_open(ref_ids): live LinkedIn state via the kaix actor's job-ID lookup (~$0.00007/job).
    jobState CLOSED / closedNotice / application limit reached -> closed.
    Jobs the actor doesn't return are "unknown" (never marked verified; re-checked next run).
"""

from __future__ import annotations

import logging
import re

from score_jobs import MAX_YEARS_ALLOWED, SENIOR_LEVELS, _years_from_text, years_from_description
from scrape_jobs import SEARCH_ACTOR_ID, run_actor

log = logging.getLogger("verify_jobs")

MIN_DESCRIPTION_CHARS = 300
MIN_NAUKRI_SUMMARY_CHARS = 100

STAFFING_INDUSTRY_RE = re.compile(r"staffing|recruit|employment services|human resources services|"
                                  r"outsourcing and offshoring|executive search", re.I)
# Company names that are agencies by their own description; "consultancy" alone is not enough
# (Tata Consultancy Services etc. are direct employers).
STAFFING_NAME_RE = re.compile(r"(?<![A-Za-z])(staffing|recruit\w*|manpower|placements?|hr solutions|hr services|"
                              r"talent acquisition|outsourcing|headhunt\w*|hr)(?![A-Za-z])", re.I)
# Hiring-for-a-client wording only. "services for our clients" is how normal firms describe their
# business, so a bare "for our clients" must NOT match.
AGENCY_TEXT_RE = re.compile(
    r"((hiring|recruiting|looking for|requirement|opening|vacancy|position)[^.]{0,60}"
    r"(for|on behalf of) (our|one of our|a leading|an? reputed) client(?![A-Za-z])"
    r"|our client(?![A-Za-z]),? (is |a |an |one )"
    r"|one of our (esteemed |reputed |leading )?clients(?![A-Za-z]) (is|are) (hiring|looking))", re.I)
RED_FLAG_RES = [
    (re.compile(r"(registration|training|joining|processing|security|placement|onboarding)\s+(fee|fees|deposit|charges?)",
                re.I), "asks for a fee or deposit"),
    (re.compile(r"(pay|paid)\s+(a|an|the)?\s*(refundable\s+)?(fee|deposit)", re.I), "asks for a fee or deposit"),
    (re.compile(r"(?<![A-Za-z])unpaid(?![A-Za-z])|no\s+(salary|stipend)|without\s+(salary|stipend)", re.I),
     "unpaid work"),
    (re.compile(r"whats\s*app\s+(your|cv|resume)|send.{0,20}resume.{0,20}whatsapp", re.I),
     "asks to apply via WhatsApp"),
]


def screen(job: dict) -> tuple[str, str] | None:
    """Return (status, reason) when the job must be excluded, else None."""
    company = job.get("company") or ""
    industry = job.get("company_industry") or ""
    desc = job.get("description") or ""
    if STAFFING_INDUSTRY_RE.search(industry):
        return "excluded_staffing", f"staffing/recruiting company (industry: {industry})"
    if STAFFING_NAME_RE.search(company):
        return "excluded_staffing", f"staffing/recruiting company name: {company}"
    if AGENCY_TEXT_RE.search(desc):
        return "excluded_staffing", "posted by an agency on behalf of a client"
    seniority = (job.get("seniority_level") or "").strip()
    if seniority.lower() in SENIOR_LEVELS:
        return "excluded_experience", f"LinkedIn seniority '{seniority}' is above fresher level"
    years = [y for y in (_years_from_text(job.get("experience_required")), years_from_description(desc))
             if y is not None]
    if years and max(years) > MAX_YEARS_ALLOWED:
        return "excluded_experience", f"requires {max(years):g}+ years of experience (max {MAX_YEARS_ALLOWED})"
    # Naukri search results only carry a short summary (structured facts are appended separately)
    core = job.get("description_core_chars")
    core_len, min_len = (core, MIN_NAUKRI_SUMMARY_CHARS) if isinstance(core, int) else (len(desc.strip()), MIN_DESCRIPTION_CHARS)
    if core_len < min_len:
        return "excluded_red_flag", f"description missing or too short ({core_len} chars)"
    for rx, reason in RED_FLAG_RES:
        if rx.search(desc):
            return "excluded_red_flag", reason
    if "company_linkedin_url" in job and not job.get("company_linkedin_url"):
        return "excluded_red_flag", "no company page linked to the posting"
    return None


def open_state(item: dict) -> tuple[bool, str]:
    if str(item.get("jobState") or "").upper() == "CLOSED" or item.get("closedNotice") or item.get("closedAt"):
        return False, "LinkedIn shows the job as closed / no longer accepting applications"
    if item.get("jobApplicationLimitReached"):
        return False, "LinkedIn application limit reached"
    return True, "open on LinkedIn"


def check_open(ref_ids: list[str], token: str, max_charge_usd: float = 0.10) -> dict[str, tuple[bool | None, str]]:
    """Live open/closed state per LinkedIn job ID. Missing results map to (None, reason)."""
    ids = list(dict.fromkeys(str(i) for i in ref_ids if i))
    if not ids:
        return {}
    items = run_actor(SEARCH_ACTOR_ID, {"jobIds": ids, "fetchDetails": True, "concurrency": 8},
                      token, len(ids), max_charge_usd)
    found = {str(it.get("jobId")): open_state(it) for it in items if isinstance(it, dict) and it.get("jobId")}
    return {i: found.get(i, (None, "LinkedIn did not return this job; open status unconfirmed")) for i in ids}
