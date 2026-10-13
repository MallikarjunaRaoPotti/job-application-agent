"""Indeed and Naukri discovery (Apify), normalized into the same job schema as LinkedIn.

Both actors search one keyword per run and support a native last-24h filter, so each configured
query becomes one capped actor run (run a few in parallel). There is no separate detail stage:
search results already carry the full description.

  Indeed  valig/indeed-jobs-scraper  (~$0.0001/job)  country=in, datePosted=1
  Naukri  valig/naukri-jobs-scraper  (~$0.0004/job)  jobAge=1, experience=<years>

job_id = sha256("<platform>:ref:<source id>")[:16], so IDs never collide across sources.
"""

from __future__ import annotations

import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from scrape_jobs import ScrapeError, _clean_str, make_job_id, run_actor

INDEED_ACTOR_ID = "TrtlecxAsNRbKl1na"   # valig/indeed-jobs-scraper
NAUKRI_ACTOR_ID = "U5tcRMY1heBYd1FYF"   # valig/naukri-jobs-scraper
IST = timezone(timedelta(hours=5, minutes=30))

log = logging.getLogger("sources")


def _html_text(markup: str | None) -> str | None:
    if not markup:
        return None
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", markup)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip() or None


def _base(platform: str, source: str, ref_id: str, link: str, title, company, now: datetime) -> dict:
    ts = now.isoformat()
    return {"job_id": make_job_id(ref_id, link, platform), "ref_id": ref_id, "source_record_id": None,
            "title": _clean_str(title), "company": _clean_str(company), "link": link, "source": source,
            "first_seen_at": ts, "last_seen_at": ts, "seniority_level": None, "apply_url": None,
            "company_industry": None, "work_mode": None, "experience_required": None, "salary": None,
            "required_skills": [], "job_status": "open"}


# ------------------------------------------------------------------ Indeed

def normalize_indeed(item: dict, now: datetime) -> dict | None:
    if not isinstance(item, dict):
        return None
    key = _clean_str(item.get("key"))
    if not key or not re.fullmatch(r"[0-9a-f]{8,32}", key):
        return None
    job = _base("indeed", "Indeed", key, f"https://in.indeed.com/viewjob?jk={key}", item.get("title"),
                (item.get("employer") or {}).get("name"), now)
    if not (job["title"] and job["company"]):
        return None
    loc = item.get("location") or {}
    job["location"] = ", ".join(p for p in (loc.get("city"), loc.get("countryName")) if p) or None
    desc = item.get("description") or {}
    job["description"] = _clean_str(desc.get("text")) or _html_text(desc.get("html"))
    job["employment_type"] = ", ".join((item.get("jobTypes") or {}).values()) or None
    sal = item.get("baseSalary") or {}
    if sal.get("min") or sal.get("max"):
        unit = (sal.get("unitOfWork") or "").lower()
        job["salary"] = f"{sal.get('currencyCode') or ''} {sal.get('min') or ''}-{sal.get('max') or ''} {unit}".strip()
    published = _clean_str(item.get("datePublished")) or _clean_str(item.get("dateOnIndeed"))
    job["posted_at"] = published
    job["posted_date"] = published[:10] if published else None
    job["company_linkedin_url"] = _clean_str((item.get("employer") or {}).get("companyPageUrl"))  # company page
    job["apply_url"] = _clean_str(item.get("jobUrl"))
    attrs = list((item.get("attributes") or {}).values())
    job["required_skills"] = attrs[:20]
    if re.search(r"remote", " ".join(attrs) + " " + (job["location"] or ""), re.I):
        job["work_mode"] = "Remote"
    job["job_status"] = "expired" if item.get("expired") else "open"
    return job


# ------------------------------------------------------------------ Naukri

def normalize_naukri(item: dict, now: datetime) -> dict | None:
    if not isinstance(item, dict):
        return None
    ref = _clean_str(item.get("id"))
    url = _clean_str(item.get("url"))
    if not ref or not re.fullmatch(r"[0-9]{6,20}", ref) or not url:
        return None
    company = item.get("company") or {}
    job = _base("naukri", "Naukri", ref, url.split("?")[0], item.get("title"), company.get("name"), now)
    if not (job["title"] and job["company"]):
        return None
    job["location"] = ", ".join(l.get("label") for l in item.get("locations") or [] if l.get("label")) or None
    summary = _html_text((item.get("description") or {}).get("full")) or ""
    job["description_core_chars"] = len(summary)   # Naukri search results only carry a short summary
    skills = item.get("skills") or {}
    edu = item.get("education") or {}
    facts = [("Role", item.get("role")), ("Role category", item.get("roleCategory")),
             ("Department", item.get("department")), ("Industry", item.get("industry")),
             ("Experience", (item.get("experience") or {}).get("text")),
             ("Education (UG)", ", ".join(edu.get("ug") or [])), ("Education (PG)", ", ".join(edu.get("pg") or [])),
             ("Key skills", ", ".join(skills.get("preferred") or [])), ("Other skills", ", ".join(skills.get("other") or []))]
    structured = "\n".join(f"{k}: {v}" for k, v in facts if v)
    job["description"] = (summary + ("\n\n" + structured if structured else "")).strip() or None
    job["employment_type"] = _clean_str(item.get("employmentType"))
    job["salary"] = _clean_str((item.get("salary") or {}).get("label"))
    if job["salary"] and re.search(r"not disclosed", job["salary"], re.I):
        job["salary"] = None
    job["experience_required"] = _clean_str((item.get("experience") or {}).get("text"))
    job["company_industry"] = "Staffing and Recruiting" if item.get("consultant") else _clean_str(item.get("industry"))
    job["company_linkedin_url"] = _clean_str(company.get("jobsUrl")) or _clean_str(company.get("logoUrl"))
    job["required_skills"] = list(dict.fromkeys((skills.get("preferred") or []) + (skills.get("other") or [])))[:20]
    created = item.get("createdDate")
    if isinstance(created, (int, float)):
        dt = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
        job["posted_at"], job["posted_date"] = dt.isoformat(), dt.astimezone(IST).date().isoformat()
    else:
        job["posted_at"] = job["posted_date"] = _clean_str(item.get("createdDateText"))
    job["work_mode"] = {"0": None, "1": "Remote", "2": "Remote", "3": "Hybrid"}.get(str(item.get("wfhType")), None)
    return job


# ------------------------------------------------------------------ search

SOURCES = {
    "indeed": {"actor": INDEED_ACTOR_ID, "normalize": normalize_indeed,
               "input": lambda q, c: {"country": "in", "title": q, "location": c.get("location", "India"),
                                      "datePosted": "1", "limit": int(c.get("max_jobs_per_query", 30))}},
    "naukri": {"actor": NAUKRI_ACTOR_ID, "normalize": normalize_naukri,
               "input": lambda q, c: {"keywords": q, "jobAge": "1", "sort": "f",
                                      "experience": int(c.get("experience_years", 1)),
                                      "limit": int(c.get("max_jobs_per_query", 30))}},
}


def search_source(name: str, queries: list[str], cfg: dict, token: str) -> list[dict]:
    """Raw items for every query (one capped actor run each, a few in parallel)."""
    spec = SOURCES[name]
    per_run_cap = float(cfg.get("max_charge_usd", 0.40)) / max(1, len(queries))
    limit = int(cfg.get("max_jobs_per_query", 30))

    def one(q):
        try:
            return run_actor(spec["actor"], spec["input"](q, cfg), token, limit, max(per_run_cap, 0.005), timeout_secs=600)
        except ScrapeError as exc:
            log.error("%s search %r failed: %s", name, q, exc)
            return []

    with ThreadPoolExecutor(max_workers=int(cfg.get("parallel_runs", 5))) as pool:
        batches = list(pool.map(one, queries))
    return [it for batch in batches for it in batch if isinstance(it, dict)]


def normalize_all(name: str, items: list[dict], now: datetime) -> tuple[list[dict], int]:
    norm = SOURCES[name]["normalize"]
    jobs, malformed = [], 0
    for it in items:
        job = norm(it, now)
        if job is None:
            malformed += 1
        else:
            jobs.append(job)
    return jobs, malformed
