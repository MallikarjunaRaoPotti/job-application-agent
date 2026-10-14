"""Company size check: openings from companies with fewer than 50 employees are excluded.

Most of those are course sellers or spam. The size comes from, in order:
  1. LinkedIn's company size on the job detail ("11-50 employees") - exact, no API call;
  2. Apollo organization enrichment by the company's domain (Company Domain column) - exact;
  3. Apollo organization enrichment by company name - fuzzy, so it is only trusted when Apollo's
     LinkedIn company page equals the job's company page (LinkedIn jobs), or Apollo's company name
     equals the job's company name (Indeed / Naukri). A name lookup can return a namesake: "Flex"
     came back as getflex.com (400 people) instead of flex.com (150,000).
An untrusted or missing answer counts as unknown, and unknown is kept: a real job is never dropped
because of a wrong match. Apollo's domain fills an empty Company Domain only when trusted.

The free Apollo plan allows organization enrichment (600 calls/day). Every company is looked up
once and cached in <workspace>/company_cache.json (keyed by company_key()); a newly known domain
triggers one more, exact lookup. A failed call (network, rate limit) is not cached, so the next run
tries again.

Usage:
  python scripts/company_size.py              # check unchecked 'Not Applied' rows on the Master tab
  python scripts/company_size.py --dry-run    # look up and print, write nothing
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

from excel_store import clean_domain, company_key

MIN_EMPLOYEES = 50
DEFAULT_MAX_LOOKUPS = 300            # per run; the free plan allows 600/day
APOLLO_ORG_URL = "https://api.apollo.io/api/v1/organizations/enrich"
CACHE_FILE = "company_cache.json"
LEGAL_WORDS = {"pvt", "private", "ltd", "limited", "inc", "llc", "llp", "plc", "corp", "corporation", "co",
               "company", "the", "gmbh"}

log = logging.getLogger("company_size")


class ApolloUnavailable(RuntimeError):
    """Apollo couldn't be reached or refused the call; the company stays unchecked for this run."""


def linkedin_size(text: str | None) -> tuple[int, int | None] | None:
    """'11-50 employees' -> (11, 50); '10,001+ employees' -> (10001, None); anything else -> None."""
    nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", text or "")]
    if not nums:
        return None
    if "+" in (text or ""):
        return nums[0], None
    return nums[0], nums[-1]


def linkedin_slug(url: str | None) -> str | None:
    """'https://in.linkedin.com/company/flexintl?trk=x' -> 'flexintl'; non-LinkedIn pages -> None."""
    m = re.search(r"linkedin\.com/company/([^/?#\s]+)", url or "", re.I)
    return m.group(1).lower() if m else None


def name_key(name: str | None) -> str:
    """Company name without punctuation and legal words: 'Zenwork, Inc.' -> 'zenwork'."""
    words = re.sub(r"[^a-z0-9]+", " ", company_key(name)).split()
    return " ".join(w for w in words if w not in LEGAL_WORDS)


def is_small(employees: int | None, size_range: tuple[int, int | None] | None) -> bool:
    if employees is not None:
        return employees < MIN_EMPLOYEES
    if size_range is not None:
        return size_range[1] is not None and size_range[1] <= MIN_EMPLOYEES   # '11-50' counts as small
    return False


def apollo_fetch(api_key: str) -> Callable[..., dict | None]:
    """A lookup function: fetch(name=..) or fetch(domain=..) -> Apollo's facts, or None if it has no record."""
    session = requests.Session()
    session.headers.update({"X-Api-Key": api_key, "Content-Type": "application/json", "Cache-Control": "no-cache"})

    def fetch(name: str | None = None, domain: str | None = None) -> dict | None:
        params = {"domain": domain} if domain else {"name": name}
        try:
            r = session.get(APOLLO_ORG_URL, params=params, timeout=30)
        except requests.RequestException as exc:
            raise ApolloUnavailable(f"Apollo unreachable: {exc}") from exc
        if r.status_code in (401, 403, 429) or r.status_code >= 500:
            raise ApolloUnavailable(f"Apollo HTTP {r.status_code}: {r.text[:200]}")
        if r.status_code != 200:
            return None
        org = (r.json() or {}).get("organization") or {}
        if not org.get("name"):
            return None
        emp = org.get("estimated_num_employees")
        return {"employees": emp if isinstance(emp, int) else None, "name": org.get("name"),
                "domain": clean_domain(org.get("primary_domain") or org.get("website_url")),
                "linkedin": linkedin_slug(org.get("linkedin_url"))}

    return fetch


class CompanySizer:
    """Size facts per company, from LinkedIn data, the cache, or one Apollo call."""

    def __init__(self, cache_path: Path, fetch: Callable[..., dict | None] | None,
                 max_lookups: int = DEFAULT_MAX_LOOKUPS):
        self.cache_path = Path(cache_path)
        self.fetch = fetch
        self.lookups_left = max_lookups
        self.lookups = 0
        self.stopped: str | None = None          # why lookups stopped early (rate limit, auth, network)
        try:
            self.cache: dict[str, dict] = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.cache = {}

    def _call(self, **kw) -> tuple[bool, dict | None]:
        """(ran, answer). ran is False when no call could be made (no key, cap reached, Apollo down)."""
        if self.fetch is None or self.lookups_left <= 0 or self.stopped:
            return False, None
        self.lookups_left -= 1
        self.lookups += 1
        try:
            return True, self.fetch(**kw)
        except ApolloUnavailable as exc:
            self.stopped = str(exc)
            log.warning("company size lookups stopped for this run: %s", exc)
            return False, None

    def info(self, company: str | None, linkedin_text: str | None = None, domain: str | None = None) -> dict | None:
        """The cached facts for a company, looking them up if needed; None if they can't be had this run."""
        key = company_key(company)
        if not key:
            return None
        cached = self.cache.get(key)
        rng = linkedin_size(linkedin_text)
        if rng is not None:
            entry = {**(cached or {}), "source": "linkedin", "employees": None, "range": list(rng), "checked_at": _now()}
            self.cache[key] = entry
            return entry
        if cached and cached.get("source") == "linkedin":
            return cached
        dom = clean_domain(domain)
        if dom and (not cached or cached.get("by_domain") != dom):
            ran, found = self._call(domain=dom)
            if ran:
                entry = {"source": "apollo" if found else "apollo:not_found", "by_domain": dom, "range": None,
                         "employees": (found or {}).get("employees"), "domain": (found or {}).get("domain") or dom,
                         "apollo_name": (found or {}).get("name"), "apollo_linkedin": (found or {}).get("linkedin"),
                         "checked_at": _now()}
                self.cache[key] = entry
                return entry
            return cached
        if cached:
            return cached
        ran, found = self._call(name=str(company))
        if not ran:
            return None
        entry = {"source": "apollo" if found else "apollo:not_found", "by_domain": None, "range": None,
                 "employees": (found or {}).get("employees"), "domain": (found or {}).get("domain"),
                 "apollo_name": (found or {}).get("name"), "apollo_linkedin": (found or {}).get("linkedin"),
                 "checked_at": _now()}
        self.cache[key] = entry
        return entry

    @staticmethod
    def trusted(entry: dict, company: str | None, company_url: str | None = None) -> bool:
        """Whether a cached answer is really about this company (see the module docstring)."""
        if entry.get("source") == "linkedin":
            return True
        if entry.get("source") != "apollo":
            return False
        if entry.get("by_domain"):
            return True
        slug = linkedin_slug(company_url)
        if slug:
            return slug == entry.get("apollo_linkedin")
        return bool(name_key(company)) and name_key(company) == name_key(entry.get("apollo_name"))

    def assess(self, company: str | None, linkedin_text: str | None = None, company_url: str | None = None,
               domain: str | None = None) -> dict | None:
        """{'employees': label, 'small': bool, 'reason': str|None, 'domain': trusted domain|None};
        None when the size couldn't be checked this run."""
        entry = self.info(company, linkedin_text, domain)
        if entry is None:
            return None
        if not self.trusted(entry, company, company_url):
            return {"employees": "unknown", "small": False, "reason": None, "domain": None}
        rng = tuple(entry["range"]) if entry.get("range") else None
        label = employees_label(entry)
        small = is_small(entry.get("employees"), rng)
        source = "LinkedIn" if entry.get("source") == "linkedin" else "Apollo"
        unit = "employee" if label == "1" else "employees"
        reason = f"{source}: {label} {unit} (under {MIN_EMPLOYEES}; likely a course seller or spam)" if small else None
        return {"employees": label, "small": small, "reason": reason,
                "domain": entry.get("domain") if entry.get("source") == "apollo" else None}

    def save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.cache, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.cache_path)


def employees_label(entry: dict | None) -> str:
    """Text for the Employees column: '2', '51-200', '10,001+' or 'unknown'."""
    if not entry:
        return "unknown"
    if entry.get("employees") is not None:
        return str(entry["employees"])
    rng = entry.get("range")
    if rng:
        return f"{rng[0]:,}+" if rng[1] is None else f"{rng[0]:,}-{rng[1]:,}"
    return "unknown"


def default_sizer(ws_root: Path, max_lookups: int = DEFAULT_MAX_LOOKUPS) -> CompanySizer:
    key = os.environ.get("APOLLO_API_KEY")
    if not key:
        log.warning("APOLLO_API_KEY is not set; company sizes come from LinkedIn data and the cache only")
    return CompanySizer(Path(ws_root) / CACHE_FILE, apollo_fetch(key) if key else None, max_lookups)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def check_companies(store, sizer: CompanySizer, items: list[tuple], upsert, fill_domains: bool = True) -> dict:
    """Check (job_id, company, LinkedIn size text, company page URL, known domain) items: record Employees,
    exclude companies under 50, and fill an empty Company Domain with a trusted Apollo domain.
    Items whose size can't be checked this run are left untouched."""
    stats = {"checked": 0, "excluded_small_company": 0, "unknown": 0, "domains_filled": 0, "excluded_ids": set()}
    for job_id, company, linkedin_text, company_url, domain in items:
        res = sizer.assess(company, linkedin_text, company_url, domain)
        if res is None:
            continue
        stats["checked"] += 1
        values = {"Employees": res["employees"]}
        if res["small"]:
            values.update({"Processing Status": "excluded_small_company", "Error Details": res["reason"]})
            stats["excluded_small_company"] += 1
            stats["excluded_ids"].add(job_id)
        elif res["employees"] == "unknown":
            stats["unknown"] += 1
        upsert(job_id, values)
        if fill_domains and res["domain"] and store.fill_company_field(company, "Company Domain", res["domain"]):
            stats["domains_filled"] += 1
    return stats


def master_items(store, details: dict[str, dict]) -> list[tuple]:
    """Size-check items for Master rows still marked Not Applied whose size isn't known yet.

    Applied and Not Working rows are the user's decision and cost no lookups. 'unknown' rows are
    passed again each run, but only a newly known Company Domain makes a new (exact) call.
    """
    from views import company_values, master_rows
    rows = store.all()
    domains = company_values(rows)
    out = []
    for r in master_rows(rows, {}):
        if r.get("Application Status") != "Not Applied" or r.get("Employees") not in (None, "", "unknown"):
            continue
        extra = details.get(str(r["Job ID"])) or {}
        domain = r.get("Company Domain") or domains.get(company_key(r.get("Company")), {}).get("Company Domain")
        out.append((str(r["Job ID"]), r.get("Company"), extra.get("company_size"), extra.get("company_linkedin_url"),
                    domain))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="look up and print, write nothing")
    parser.add_argument("--max-lookups", type=int, default=DEFAULT_MAX_LOOKUPS)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except ImportError:
        pass
    from excel_store import JobStore
    from run_pipeline import discovery_details
    from workspace import get_workspace
    ws = get_workspace()
    store = JobStore(ws.jobs_xlsx)
    items = master_items(store, discovery_details(ws, []))
    sizer = default_sizer(ws.root, args.max_lookups)
    if args.dry_run:
        for _, company, size_text, url, domain in items:
            res = sizer.assess(company, size_text, url, domain) or {}
            print(f"{res.get('employees', 'unchecked'):>10}  {company}  -> {'EXCLUDE' if res.get('small') else 'keep'}")
        return 0
    stats = check_companies(store, sizer, items, store.upsert)
    sizer.save()
    store.save()
    stats["excluded_ids"] = len(stats["excluded_ids"])
    print(json.dumps({**stats, "apollo_lookups": sizer.lookups, "stopped": sizer.stopped}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
