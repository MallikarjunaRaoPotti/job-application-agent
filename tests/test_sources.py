"""Indeed / Naukri normalization, multi-source pipeline, and the per-source 24 h views."""
from datetime import datetime, timedelta, timezone

from openpyxl import load_workbook

import run_pipeline as rp
from excel_store import JobStore
from scrape_jobs import make_job_id
from sources import normalize_indeed, normalize_naukri
from test_pipeline import LONG_DESC, RAW, FakeLLM, evaluation, rows, top_eval
from views import posted_within_24h, source_of
from workspace import get_workspace

NOW = datetime.now(timezone.utc)
IST = timezone(timedelta(hours=5, minutes=30))


def indeed_item(key="a1b2c3d4e5f60718", expired=False, published=None, title="Junior Data Analyst"):
    return {"key": key, "title": title, "url": f"https://in.indeed.com/viewjob?jk={key}",
            "employer": {"name": "Acme Analytics", "companyPageUrl": "https://in.indeed.com/cmp/Acme"},
            "location": {"city": "Pune", "countryName": "India"}, "description": {"text": LONG_DESC},
            "jobTypes": {"x": "Full-time"},
            "baseSalary": {"min": 300000, "max": 500000, "unitOfWork": "YEAR", "currencyCode": "INR"},
            "datePublished": published or (NOW - timedelta(hours=3)).isoformat(), "expired": expired,
            "attributes": {"a": "SQL", "b": "Power BI"}}


def naukri_item(ref="131026048999", consultant=False, created=None, summary=None):
    created = created if created is not None else (NOW - timedelta(hours=2)).timestamp() * 1000
    return {"id": ref, "title": "Data Analyst - Fresher",
            "url": f"https://www.naukri.com/job-listings-data-analyst-{ref}?src=x",
            "company": {"name": "Beta Corp", "jobsUrl": "https://www.naukri.com/beta-jobs"},
            "description": {"full": summary or ("<p>Analyse sales data with SQL and Power BI, build dashboards, "
                                                "clean and validate data, and report weekly insights to the business team.</p>")},
            "experience": {"text": "0-1 Yrs"}, "salary": {"label": "3-4 Lacs"}, "locations": [{"label": "Hyderabad"}],
            "skills": {"preferred": ["SQL"], "other": ["Power BI"]}, "education": {"ug": ["B.Tech/B.E."]},
            "role": "Data Analyst", "industry": "IT Services", "consultant": consultant, "createdDate": created,
            "employmentType": "Full Time, Permanent", "wfhType": "0"}


# ---- normalization

def test_indeed_normalized_with_namespaced_id():
    j = normalize_indeed(indeed_item(), NOW)
    assert j["ref_id"] == "a1b2c3d4e5f60718" and j["source"] == "Indeed"
    assert j["job_id"] == make_job_id("a1b2c3d4e5f60718", None, "indeed") != make_job_id("a1b2c3d4e5f60718", None)
    assert j["link"] == "https://in.indeed.com/viewjob?jk=a1b2c3d4e5f60718" and j["location"] == "Pune, India"
    assert normalize_indeed(indeed_item(expired=True), NOW)["job_status"] == "expired"
    assert normalize_indeed({"key": "a1b2c3d4", "title": "x", "employer": {}}, NOW) is None


def test_naukri_normalized_with_structured_facts_and_staffing_flag():
    j = normalize_naukri(naukri_item(), NOW)
    assert j["source"] == "Naukri" and j["link"].endswith("-131026048999") and "?" not in j["link"]
    assert j["experience_required"] == "0-1 Yrs" and "Key skills: SQL" in j["description"]
    assert j["description_core_chars"] < 300
    assert normalize_naukri(naukri_item(consultant=True), NOW)["company_industry"] == "Staffing and Recruiting"


def test_source_of_url():
    assert source_of("https://in.indeed.com/viewjob?jk=1") == "Indeed"
    assert source_of("https://www.naukri.com/job-listings-x-1") == "Naukri"
    assert source_of("https://www.linkedin.com/jobs/view/1") == "LinkedIn"


# ---- 24 h window

def test_posted_within_24h():
    assert posted_within_24h((NOW - timedelta(hours=5)).isoformat(), NOW)
    assert not posted_within_24h((NOW - timedelta(hours=30)).isoformat(), NOW)
    today = NOW.astimezone(IST).date()
    assert posted_within_24h(today.isoformat(), NOW) and posted_within_24h((today - timedelta(days=1)).isoformat(), NOW)
    assert not posted_within_24h((today - timedelta(days=2)).isoformat(), NOW)
    assert not posted_within_24h(None, NOW)


# ---- pipeline + views

def run_multi(llm, source_items, **kw):
    ws = get_workspace()
    cfg = {"sources": {"indeed": {"enabled": True}, "naukri": {"enabled": True}}}
    stats = rp.run(ws, run_id=kw.pop("run_id", "r1"), client_factory=llm.factory, search_config=cfg,
                   raw_items=kw.pop("raw_items", []), source_items=source_items, tailor=False, **kw)
    return ws, stats


def _view_titles(wb, sheet):
    s = wb[sheet]
    return [s.cell(row=r, column=4).value for r in range(5, s.max_row + 1) if s.cell(row=r, column=14).value]


def test_indeed_and_naukri_jobs_flow_through_screen_score_and_views():
    items = {"indeed": [indeed_item(), indeed_item(key="ffff0000eeee1111", title="Data Analyst Intern")],
             "naukri": [naukri_item(), naukri_item(ref="131026048111", consultant=True)]}
    llm = FakeLLM(evaluations=[top_eval(), evaluation((3, 3, 3, 3)), evaluation()])
    checked = []
    ws, stats = run_multi(llm, items, open_checker=lambda ids: checked.extend(ids) or {})
    assert stats["sources"]["indeed"]["eligible"] == 2 and stats["sources"]["naukri"]["eligible"] == 2
    assert stats["excluded_staffing"] == 1 and llm.calls["submit_evaluation"] == 3
    assert checked == []                                  # LinkedIn open-check never sees Indeed/Naukri IDs
    data = rows(ws)
    assert sum(1 for r in data.values() if r["Processing Status"] == "verified_top_match") == 1

    wb = load_workbook(ws.jobs_xlsx)
    assert wb.sheetnames[:4] == ["Dashboard", "LinkedIn", "Indeed", "Naukri"] and "Jobs" in wb.sheetnames
    shown = {s: _view_titles(wb, s) for s in ("Indeed", "Naukri", "LinkedIn")}
    assert len(shown["Indeed"]) + len(shown["Naukri"]) == 2           # only the two 8+ jobs
    assert shown["LinkedIn"] == []
    assert len(data) == 4                                              # Jobs sheet keeps everything


def test_view_excludes_old_postings_but_jobs_keeps_them():
    items = {"indeed": [indeed_item(published=(NOW - timedelta(hours=40)).isoformat())], "naukri": []}
    ws, _ = run_multi(FakeLLM(evaluations=[top_eval()]), items)
    assert _view_titles(load_workbook(ws.jobs_xlsx), "Indeed") == []  # 40 h old -> not in the 24 h view
    assert len(rows(ws)) == 1                                          # but kept in Jobs


def test_rerun_dedups_indeed_and_naukri():
    items = {"indeed": [indeed_item()], "naukri": [naukri_item()]}
    ws, _ = run_multi(FakeLLM(evaluations=[evaluation(), evaluation()]), items)
    _, stats = run_multi(FakeLLM(), items, run_id="r2")
    assert stats["sources"]["indeed"]["previously_seen"] == 1 and stats["sources"]["naukri"]["previously_seen"] == 1
    assert len(JobStore(ws.jobs_xlsx)) == 2


def test_linkedin_still_works_alongside_sources():
    ws, stats = run_multi(FakeLLM(evaluations=[evaluation(), evaluation((3, 3, 3, 3))]),
                          {"indeed": [], "naukri": []}, raw_items=RAW)
    assert stats["discovery"]["eligible"] == 2 and stats["evaluated"] == 2
