"""Job discovery: Apify LinkedIn search -> detail scrape -> normalize -> dedup.

Two Apify actors are chained:
  1. kaix/linkedin-jobs-scraper (SEARCH_ACTOR_ID): keyword x location search in search-only
     mode (no details). Very cheap; used only to collect LinkedIn job IDs.
  2. codenest/linkedin-job-search-scraper (DETAIL_ACTOR_ID): full job details per job ID.
     It cannot search by keyword; it only scrapes IDs present in /jobs/view/{id} URLs.
IDs already in the job index are dropped between the two stages so details are never re-bought.

The dedup index comes from jobs.xlsx (excel_store.JobStore.dedup_index()); run via run_pipeline.py.
"""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

import requests

SEARCH_ACTOR_ID = "Ps256r1kXCXniIIYE"  # kaix/linkedin-jobs-scraper
DETAIL_ACTOR_ID = "WeCImTcc8jwzbpv7g"  # codenest/linkedin-job-search-scraper
APIFY_API = "https://api.apify.com/v2"
SOURCE = "Apify"
PLATFORM = "linkedin"  # namespace for job_id derivation; ref_id is a LinkedIn job ID


TERMINAL_RUN_STATES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
# Titles that can never pass the strict 0-1 year rule; skipped before paying for details.
_W = r"(?<![A-Za-z])"  # letter boundaries (avoids \b, which shell heredocs mangle)
_E = r"(?![A-Za-z])"
SENIOR_TITLE_RE = re.compile(_W + r"(senior|sr|lead|principal|staff|manager|head|director|architect|vp|"
                             r"chief|ii|iii|iv)" + _E, re.I)
JUNIOR_TITLE_RE = re.compile(_W + r"(junior|jr|fresher|freshers|graduate|grad|intern|internship|trainee|"
                             r"entry|associate|apprentice)" + _E, re.I)
LINKEDIN_JOB_ID_RE = re.compile(r"/jobs/view/(?:[^/?#]*-)?(\d{6,})")

log = logging.getLogger("scrape_jobs")


class ScrapeError(RuntimeError):
    """Unrecoverable discovery failure (bad token, actor failed, etc.)."""


# ---------------------------------------------------------------- identifiers

def canonical_url(url: str | None) -> str | None:
    if not url or not isinstance(url, str):
        return None
    url = url.strip()
    m = LINKEDIN_JOB_ID_RE.search(url)
    if m and "linkedin.com" in url:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}"
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def extract_ref_id(item: dict) -> str | None:
    """LinkedIn job ID from the record, falling back to the job link. Never invented."""
    raw = item.get("id")
    if raw is not None and re.fullmatch(r"\d+", str(raw).strip()):
        return str(raw).strip()
    link = item.get("link")
    if isinstance(link, str) and "linkedin.com" in link:
        m = LINKEDIN_JOB_ID_RE.search(link)
        if m:
            return m.group(1)
    return None


def make_job_id(ref_id: str | None, url: str | None, platform: str = PLATFORM) -> str | None:
    """Deterministic internal ID: hash of platform + ref_id, else platform + canonical URL.

    The default platform is linkedin, so LinkedIn IDs are unchanged; other sources pass their own name.
    """
    if ref_id:
        key = f"{platform}:ref:{ref_id}"
    elif canonical_url(url):
        key = f"{platform}:url:{canonical_url(url)}"
    else:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------- normalizing

def _clean_str(value) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _parse_date(value) -> date | None:
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _salary(item: dict) -> str | None:
    sal = item.get("salary")
    if isinstance(sal, dict):
        text = _clean_str(sal.get("formattedSalary"))
        if text:
            return text
    elif isinstance(sal, str):
        return _clean_str(sal)
    return _clean_str(item.get("salaryText"))


def _work_mode(item: dict) -> str | None:
    wt = _clean_str(item.get("workplaceType"))
    if wt:
        return wt.title()
    loc = (item.get("location") or "").lower()
    if "remote" in loc:
        return "Remote"
    if "hybrid" in loc:
        return "Hybrid"
    return None


def _apply_url(item: dict) -> str | None:
    """External apply link; drops LinkedIn sign-in/sign-up redirects, which aren't application URLs."""
    url = _clean_str(item.get("applyUrl"))
    if url and re.search(r"linkedin\.com/(signup|login|uas/login|authwall)", url):
        return None
    return url


def job_status(item: dict, today: date) -> str:
    """'expired' only when the source says closed or a closing date is past; otherwise 'open'."""
    if str(item.get("jobState") or "").upper() == "CLOSED" or item.get("closedNotice"):
        return "expired"
    for field in ("validThrough", "applicationDeadline"):
        d = _parse_date(item.get(field))
        if d and d < today:
            return "expired"
    return "open"


def normalize(item: dict, now: datetime) -> dict | None:
    """Map one actor record to the PRD schema. Returns None for malformed records."""
    if not isinstance(item, dict):
        return None
    title = _clean_str(item.get("title"))
    company = _clean_str(item.get("companyName"))
    ref_id = extract_ref_id(item)
    link = canonical_url(item.get("link")) or (
        f"https://www.linkedin.com/jobs/view/{ref_id}" if ref_id else None
    )
    job_id = make_job_id(ref_id, link)
    if not (title and company and link and job_id):
        return None

    ts = now.isoformat()
    return {
        "job_id": job_id,
        "ref_id": ref_id,
        "source_record_id": None,  # actor exposes no separate Apify record ID
        "title": title,
        "company": company,
        "link": link,
        "description": _clean_str(item.get("descriptionText")),
        "location": _clean_str(item.get("location")),
        "work_mode": _work_mode(item),
        "employment_type": _clean_str(item.get("employmentType")),
        "experience_required": _clean_str(item.get("experienceRequirement")),
        "salary": _salary(item),
        "posted_date": _clean_str(item.get("datePosted")),
        "source": SOURCE,
        "first_seen_at": ts,
        "last_seen_at": ts,
        "job_status": job_status(item, now.date()),
        # Extra actor fields useful for scoring; not part of the identity.
        "seniority_level": _clean_str(item.get("seniorityLevel")),
        "required_skills": item.get("requiredSkills") or item.get("skills") or [],
        "apply_url": _apply_url(item),
        "company_industry": _clean_str(item.get("companyIndustry")) or _clean_str(item.get("industries")),
        "company_linkedin_url": _clean_str(item.get("companyLinkedinUrl")),
    }


# ------------------------------------------------------------------ dedup

def _same_posting(a: dict, b: dict) -> bool:
    norm = lambda s: re.sub(r"\W+", " ", (s or "").lower()).strip()
    return norm(a.get("company")) == norm(b.get("company")) and norm(a.get("title")) == norm(b.get("title"))


def deduplicate(jobs: list[dict], index: dict) -> tuple[list[dict], dict]:
    """Split normalized jobs into new/eligible vs. seen. Mutates `index` in place.

    - Same job_id twice in one run -> in-run duplicate.
    - job_id already in index -> previously seen; keeps first_seen_at, bumps last_seen_at.
    - Same job_id but different company/title -> conflict, flagged, not merged.
    """
    result = {"new": [], "duplicate_in_run": [], "previously_seen": [], "conflicts": [], "expired": []}
    seen_this_run: set[str] = set()
    for job in jobs:
        jid = job["job_id"]
        if jid in seen_this_run:
            result["duplicate_in_run"].append(job)
            continue
        seen_this_run.add(jid)

        prior = index.get(jid)
        if prior and not _same_posting(prior, job):
            job["job_status"] = "conflict"
            result["conflicts"].append({"job": job, "existing": prior})
            continue
        if prior:
            job["first_seen_at"] = prior["first_seen_at"]
            prior["last_seen_at"] = job["last_seen_at"]
            prior["job_status"] = job["job_status"]
            result["previously_seen"].append(job)
            continue

        index[jid] = {k: job[k] for k in ("ref_id", "title", "company", "link", "first_seen_at", "last_seen_at", "job_status")}
        if job["job_status"] == "expired":
            result["expired"].append(job)
        else:
            result["new"].append(job)
    return result["new"], result


# ------------------------------------------------------------------ Apify

def _request(method: str, url: str, token: str, retries: int = 3, **kw) -> requests.Response:
    headers = {"Authorization": f"Bearer {token}"}
    for attempt in range(1, retries + 1):
        try:
            resp = requests.request(method, url, headers=headers, timeout=90, **kw)
        except requests.RequestException as exc:
            if attempt == retries:
                raise ScrapeError(f"Apify request failed: {exc.__class__.__name__}") from exc
        else:
            if resp.status_code in (401, 403):
                raise ScrapeError(f"Apify auth failed (HTTP {resp.status_code}); check APIFY_API_TOKEN")
            if resp.status_code < 500 and resp.status_code != 429:
                if not resp.ok:
                    raise ScrapeError(f"Apify HTTP {resp.status_code}: {resp.text[:300]}")
                return resp
            if attempt == retries:
                raise ScrapeError(f"Apify HTTP {resp.status_code} after {retries} attempts")
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def run_actor(actor_id: str, actor_input: dict, token: str, max_items: int,
              max_charge_usd: float, timeout_secs: int = 1800) -> list[dict]:
    """Start an actor run with a hard spend cap, wait for it, return dataset items."""
    resp = _request(
        "POST", f"{APIFY_API}/acts/{actor_id}/runs", token,
        params={"maxItems": max_items, "maxTotalChargeUsd": max_charge_usd,
                "timeout": timeout_secs, "memory": 1024},
        json=actor_input,
    )
    run = resp.json()["data"]
    log.info("Apify run started: actor=%s run=%s", actor_id, run["id"])
    deadline = time.monotonic() + timeout_secs + 120
    while run["status"] not in TERMINAL_RUN_STATES:
        if time.monotonic() > deadline:
            raise ScrapeError(f"Apify run {run['id']} did not finish in time")
        run = _request("GET", f"{APIFY_API}/actor-runs/{run['id']}", token,
                       params={"waitForFinish": 60}).json()["data"]
    if run["status"] != "SUCCEEDED":
        # A run stopped by its own spend cap or time limit still holds paid-for results: keep them.
        capped = run["status"] in ("ABORTED", "TIMED-OUT") and (
            run["status"] == "TIMED-OUT" or "maximum cost" in str(run.get("statusMessage") or ""))
        if not capped:
            raise ScrapeError(f"Apify run {run['id']} ended with status {run['status']}")
        log.warning("Apify run %s stopped early (%s); keeping its partial results", run["id"],
                    run.get("statusMessage") or run["status"])
    items = dataset_items(run["defaultDatasetId"], token)
    log.info("Apify run %s returned %d items (cost ~$%.4f)", run["id"], len(items),
             run.get("usageTotalUsd") or 0)
    return items


def dataset_items(dataset_id: str, token: str) -> list[dict]:
    return _request("GET", f"{APIFY_API}/datasets/{dataset_id}/items", token,
                    params={"clean": "true", "format": "json"}).json()


def read_urls_file(path: Path) -> list[str]:
    if not path.exists():
        return []
    urls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            urls.append(line)
    return urls


def ids_from_url(url: str) -> list[str]:
    """LinkedIn job IDs the detail actor can extract from a URL. Keyword-search URLs yield none."""
    ids = []
    m = LINKEDIN_JOB_ID_RE.search(url)
    if m:
        ids.append(m.group(1))
    q = parse_qs(urlparse(url).query)
    for value in q.get("currentJobId", []) + q.get("originToLandingJobPostings", []):
        ids.extend(v.strip() for v in value.split(",") if re.fullmatch(r"\d{6,}", v.strip()))
    return list(dict.fromkeys(ids))


def load_search_config(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def search_job_ids(cfg: dict, token: str) -> list[dict]:
    """Stage 1: keyword x location search. Returns raw search hits (may repeat across queries)."""
    queries = cfg.get("queries") or []
    if not queries:
        return []
    max_results = int(cfg.get("max_search_results", 300))
    actor_input = {
        "queries": queries,
        "locations": cfg.get("locations") or [],
        "datePosted": cfg.get("date_posted", "past_24h"),
        "maxJobs": max_results,
        "maxJobsPerSearch": int(cfg.get("max_jobs_per_search", 25)),
        "fetchDetails": False,
    }
    hits = run_actor(SEARCH_ACTOR_ID, actor_input, token, max_results,
                     float(cfg.get("max_charge_usd_search", 0.10)))
    return [h for h in hits if isinstance(h, dict) and re.fullmatch(r"\d{6,}", str(h.get("jobId") or ""))]


def fetch_details(job_ids: list[str], token: str, max_charge_usd: float) -> list[dict]:
    """Stage 2: full job details from the detail actor, one /jobs/view/{id} URL per job."""
    if not job_ids:
        return []
    urls = [{"url": f"https://www.linkedin.com/jobs/view/{i}"} for i in job_ids]
    return run_actor(DETAIL_ACTOR_ID, {"urls": urls}, token, len(job_ids), max_charge_usd)


def _html_to_text(markup: str | None) -> str | None:
    if not markup:
        return None
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>", "\n", markup)
    text = re.sub(r"(?i)<li[^>]*>", "- ", text)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def kaix_to_detail(item: dict) -> dict:
    """Map a kaix job-ID lookup record onto the codenest field names normalize() expects."""
    app = item.get("applicationDetails") or {}
    return {
        "id": item.get("jobId"), "title": item.get("title"), "companyName": item.get("company"),
        "location": item.get("location"), "link": item.get("jobUrl"),
        "descriptionText": _html_to_text(item.get("descriptionHtml")) or item.get("description"),
        "employmentType": item.get("employmentType"), "seniorityLevel": item.get("experienceLevel"),
        "datePosted": item.get("postedDate"), "companyLinkedinUrl": item.get("companyUrl"),
        "companyIndustry": item.get("industries"), "applyUrl": app.get("companyApplyUrl"),
        "validThrough": item.get("expireAt"), "jobState": item.get("jobState"),
        "closedNotice": item.get("closedNotice"), "detailSource": "kaix",
    }


def fetch_details_fallback(job_ids: list[str], token: str, max_charge_usd: float = 0.05) -> list[dict]:
    """Details for jobs the primary detail actor silently dropped, via the kaix job-ID lookup."""
    if not job_ids:
        return []
    items = run_actor(SEARCH_ACTOR_ID, {"jobIds": job_ids, "fetchDetails": True, "concurrency": 8},
                      token, len(job_ids), max_charge_usd)
    return [kaix_to_detail(it) for it in items if isinstance(it, dict) and it.get("jobId")]


# ------------------------------------------------------------------ pipeline


def discover(urls: list[str] | None = None, raw_items: list[dict] | None = None, *,
             search_config: dict | None = None, token: str | None = None,
             index: dict | None = None, snapshot_dir: Path | None = None,
             dry_run: bool = False, run_id: str | None = None,
             search_hits_override: list[dict] | None = None) -> dict:
    """Run discovery end to end. Pass `raw_items` (detail-actor output) to skip Apify entirely.

    `index` ({job_id: {...}}, from jobs.xlsx) is consulted and updated in memory; the caller persists it.
    """
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    now = datetime.now(timezone.utc)
    cfg = search_config or {}
    stats = {"run_id": run_id, "search_hits": 0, "manual_urls": len(urls or []),
             "urls_skipped_unsupported": [], "discovered": 0, "duplicate_in_run": 0,
             "previously_seen": 0, "skipped_senior_titles": 0, "details_requested": 0, "details_fallback": 0, "details_capped": 0,
             "details_returned": 0, "malformed": 0, "normalized": 0, "conflicts": 0,
             "expired": 0, "eligible": 0}
    index = {} if index is None else index
    search_hits: list[dict] = []

    if raw_items is None:
        # Collect candidate IDs from search + manual URLs, in order.
        candidate_ids: list[str] = []
        if search_hits_override is not None or cfg.get("queries"):
            if not token:
                raise ScrapeError("APIFY_API_TOKEN is not set")
            if search_hits_override is not None:  # reuse an already-paid search dataset
                search_hits = [h for h in search_hits_override if isinstance(h, dict)
                               and re.fullmatch(r"[0-9]{6,}", str(h.get("jobId") or ""))]
            else:
                search_hits = search_job_ids(cfg, token)
            stats["search_hits"] = len(search_hits)
            titles = {str(h["jobId"]): str(h.get("title") or "") for h in search_hits}
            senior = {i for i, t in titles.items() if SENIOR_TITLE_RE.search(t)}
            stats["skipped_senior_titles"] = len(senior)
            # fresher-sounding titles first, so the detail cap is spent where qualification is likeliest
            hit_ids = [str(h["jobId"]) for h in search_hits if str(h["jobId"]) not in senior]
            candidate_ids += sorted(hit_ids, key=lambda i: 0 if JUNIOR_TITLE_RE.search(titles[i]) else 1)
        for url in urls or []:
            ids = ids_from_url(url)
            if not ids:
                stats["urls_skipped_unsupported"].append(url)
            candidate_ids += ids
        if stats["urls_skipped_unsupported"]:
            log.warning("[%s] skipping %d URLs with no extractable job ID", run_id,
                        len(stats["urls_skipped_unsupported"]))

        stats["discovered"] = len(candidate_ids)
        unique_ids = list(dict.fromkeys(candidate_ids))
        stats["duplicate_in_run"] = len(candidate_ids) - len(unique_ids)

        # Don't pay for details of jobs already indexed; just record that we saw them again.
        new_ids = []
        for ref_id in unique_ids:
            jid = make_job_id(ref_id, None)
            if jid in index:
                index[jid]["last_seen_at"] = now.isoformat()
                stats["previously_seen"] += 1
            else:
                new_ids.append(ref_id)

        cap = int(cfg.get("max_detail_fetches", 60))
        stats["details_capped"] = max(0, len(new_ids) - cap)
        new_ids = new_ids[:cap]
        stats["details_requested"] = len(new_ids)
        if stats["details_capped"]:
            log.warning("[%s] %d new jobs over max_detail_fetches=%d were not fetched this run",
                        run_id, stats["details_capped"], cap)
        raw_items = []
        if new_ids:
            if not token:
                raise ScrapeError("APIFY_API_TOKEN is not set")
            try:
                raw_items = fetch_details(new_ids, token, float(cfg.get("max_charge_usd_details", 0.50)))
            except ScrapeError as exc:  # the primary detail actor crashing must not lose the day's jobs
                log.error("[%s] detail actor failed (%s); using fallback for all %d jobs", run_id, exc, len(new_ids))
                raw_items = []
            got = {extract_ref_id(i) for i in raw_items if isinstance(i, dict)}
            dropped = [i for i in new_ids if i not in got]
            if dropped:
                log.warning("[%s] detail actor returned %d of %d jobs; fetching %d via fallback",
                            run_id, len(new_ids) - len(dropped), len(new_ids), len(dropped))
                fallback = fetch_details_fallback(dropped, token, float(cfg.get("max_charge_usd_fallback", 0.30)))
                stats["details_fallback"] = len(fallback)
                raw_items = raw_items + fallback
        stats["details_returned"] = len(raw_items)
        if len(raw_items) < len(new_ids):
            log.warning("[%s] details returned for %d of %d requested jobs", run_id, len(raw_items), len(new_ids))
    else:
        stats["discovered"] = len(raw_items)

    normalized = []
    for item in raw_items:
        job = normalize(item, now)
        if job is None:
            stats["malformed"] += 1
            log.warning("[%s] malformed record skipped (id=%s)", run_id,
                        item.get("id") if isinstance(item, dict) else None)
        else:
            normalized.append(job)
    stats["normalized"] = len(normalized)

    eligible, buckets = deduplicate(normalized, index)
    stats["duplicate_in_run"] += len(buckets["duplicate_in_run"])
    stats["previously_seen"] += len(buckets["previously_seen"])
    stats["conflicts"] = len(buckets["conflicts"])
    stats["expired"] = len(buckets["expired"])
    stats["eligible"] = len(eligible)
    for c in buckets["conflicts"]:
        log.warning("[%s] job_id=%s ref_id=%s conflicts with indexed record; flagged, not merged",
                    run_id, c["job"]["job_id"], c["job"]["ref_id"])

    if snapshot_dir and not dry_run:
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / f"{run_id}_discovery.json").write_text(json.dumps(
            {"run_id": run_id, "stats": stats, "search_hits": search_hits, "raw_items": raw_items,
             "conflicts": buckets["conflicts"]}, indent=1, ensure_ascii=False), encoding="utf-8")

    log.info("[%s] %d discovered -> %d details fetched -> %d normalized -> %d eligible "
             "(%d dup in run, %d previously seen, %d expired, %d malformed, %d conflicts, %d over cap)",
             run_id, stats["discovered"], stats["details_returned"], stats["normalized"],
             stats["eligible"], stats["duplicate_in_run"], stats["previously_seen"], stats["expired"],
             stats["malformed"], stats["conflicts"], stats["details_capped"])
    return {"run_id": run_id, "stats": stats, "eligible": eligible, "buckets": buckets}
