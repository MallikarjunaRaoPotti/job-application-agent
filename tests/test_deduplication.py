import json
from pathlib import Path

from scrape_jobs import deduplicate, discover

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "apify_linkedin_sample.json").read_text(encoding="utf-8"))


def run(index, items=FIXTURE):
    return discover(raw_items=items, index=index)


def test_counts_from_fixture():
    s = run({})["stats"]
    assert s["discovered"] == 6
    assert s["malformed"] == 2          # missing title + non-dict
    assert s["normalized"] == 4
    assert s["duplicate_in_run"] == 1   # 4100000001 twice
    assert s["expired"] == 1            # 4100000002
    assert s["eligible"] == 2


def test_rerun_keeps_job_id_and_first_seen():
    index = {}
    first = run(index)
    second = run(index)
    assert second["stats"]["eligible"] == 0
    assert second["stats"]["previously_seen"] == 3
    ids_first = {j["job_id"]: j["first_seen_at"] for j in first["eligible"]}
    for job in second["buckets"]["previously_seen"]:
        if job["job_id"] in ids_first:
            assert job["first_seen_at"] == ids_first[job["job_id"]]


def test_same_title_company_different_postings_not_merged():
    items = [
        {"id": "4200000001", "title": "Data Analyst", "companyName": "Same Co", "link": "https://www.linkedin.com/jobs/view/4200000001"},
        {"id": "4200000002", "title": "Data Analyst", "companyName": "Same Co", "link": "https://www.linkedin.com/jobs/view/4200000002"},
    ]
    assert run({}, items)["stats"]["eligible"] == 2


def test_conflicting_record_flagged_not_merged():
    index = {"abc": {"ref_id": "1", "title": "Data Engineer", "company": "Acme",
                     "link": "x", "first_seen_at": "t0", "last_seen_at": "t0", "job_status": "open"}}
    job = {"job_id": "abc", "ref_id": "1", "title": "Sales Manager", "company": "Other",
           "link": "x", "first_seen_at": "t1", "last_seen_at": "t1", "job_status": "open"}
    eligible, buckets = deduplicate([job], index)
    assert eligible == [] and len(buckets["conflicts"]) == 1
    assert index["abc"]["title"] == "Data Engineer"


def test_empty_results_finish_gracefully():
    assert run({}, [])["stats"]["eligible"] == 0


def test_unsupported_urls_skipped_without_api_call():
    r = discover(["https://www.linkedin.com/jobs/search/?keywords=Data%20Engineer&location=India"], token=None)
    assert len(r["stats"]["urls_skipped_unsupported"]) == 1 and r["stats"]["details_requested"] == 0


def test_ids_from_url():
    from scrape_jobs import ids_from_url
    assert ids_from_url("https://in.linkedin.com/jobs/view/data-analyst-at-x-4476016341?trk=a") == ["4476016341"]
    assert ids_from_url("https://www.linkedin.com/jobs/search/?currentJobId=4377392388"
                        "&originToLandingJobPostings=4380097715%2C4377392388") == ["4377392388", "4380097715"]
    assert ids_from_url("https://www.linkedin.com/jobs/search/?keywords=Data%20Engineer") == []


def test_search_then_details_skips_known_ids(monkeypatch):
    import scrape_jobs
    hits = [{"jobId": "4300000001"}, {"jobId": "4300000002"}, {"jobId": "4300000001"}]
    fetched = []

    def fake_details(ids, token, cap):
        fetched.append(list(ids))
        return [{"id": i, "title": "Data Analyst", "companyName": f"Co {i}",
                 "link": f"https://www.linkedin.com/jobs/view/{i}"} for i in ids]

    monkeypatch.setattr(scrape_jobs, "search_job_ids", lambda cfg, token: hits)
    monkeypatch.setattr(scrape_jobs, "fetch_details", fake_details)
    kw = dict(search_config={"queries": ["x"]}, token="t", index={})

    first = discover(**kw)["stats"]
    assert fetched[-1] == ["4300000001", "4300000002"]
    assert first["discovered"] == 3 and first["duplicate_in_run"] == 1 and first["eligible"] == 2

    hits.append({"jobId": "4300000003"})
    second = discover(**kw)["stats"]
    assert fetched[-1] == ["4300000003"]          # known IDs never re-fetched
    assert second["previously_seen"] == 2 and second["eligible"] == 1


def test_detail_fetch_cap(monkeypatch):
    import scrape_jobs
    monkeypatch.setattr(scrape_jobs, "search_job_ids", lambda cfg, token: [{"jobId": str(4300000100 + i)} for i in range(5)])
    monkeypatch.setattr(scrape_jobs, "fetch_details", lambda ids, token, cap: [])
    monkeypatch.setattr(scrape_jobs, "fetch_details_fallback", lambda ids, token, cap=0.05: [])
    s = discover(search_config={"queries": ["x"], "max_detail_fetches": 2}, token="t")["stats"]
    assert s["details_requested"] == 2 and s["details_capped"] == 3


def test_senior_titles_skipped_and_junior_titles_fetched_first(monkeypatch):
    import scrape_jobs
    hits = [{"jobId": "4300000201", "title": "Data Engineer"},
            {"jobId": "4300000202", "title": "Senior Data Engineer"},
            {"jobId": "4300000203", "title": "Data Analyst Intern"},
            {"jobId": "4300000204", "title": "Lead AI Engineer"},
            {"jobId": "4300000205", "title": "Junior Data Analyst"}]
    fetched = []
    monkeypatch.setattr(scrape_jobs, "search_job_ids", lambda cfg, token: hits)
    monkeypatch.setattr(scrape_jobs, "fetch_details", lambda ids, token, cap: fetched.extend(ids) or [])
    monkeypatch.setattr(scrape_jobs, "fetch_details_fallback", lambda ids, token, cap=0.05: [])
    s = discover(search_config={"queries": ["x"], "max_detail_fetches": 2}, token="t")["stats"]
    assert s["skipped_senior_titles"] == 2
    assert fetched == ["4300000203", "4300000205"]          # junior-sounding titles win the detail cap


def test_detail_fallback_fills_jobs_the_primary_actor_dropped(monkeypatch):
    import scrape_jobs
    hits = [{"jobId": "4300000301", "title": "Data Analyst"}, {"jobId": "4300000302", "title": "Junior Data Engineer"}]
    monkeypatch.setattr(scrape_jobs, "search_job_ids", lambda cfg, token: hits)
    monkeypatch.setattr(scrape_jobs, "fetch_details", lambda ids, token, cap: [
        {"id": "4300000301", "title": "Data Analyst", "companyName": "A", "link": "https://www.linkedin.com/jobs/view/4300000301"}])
    asked = []

    def fallback(ids, token, cap=0.05):
        asked.extend(ids)
        return [scrape_jobs.kaix_to_detail({"jobId": "4300000302", "title": "Junior Data Engineer", "company": "B",
                                            "jobUrl": "https://in.linkedin.com/jobs/view/junior-data-engineer-4300000302",
                                            "descriptionHtml": "<p>Build&nbsp;pipelines</p><ul><li>SQL</li></ul>",
                                            "experienceLevel": "Entry level", "industries": "Software Development",
                                            "companyUrl": "https://www.linkedin.com/company/b"})]
    monkeypatch.setattr(scrape_jobs, "fetch_details_fallback", fallback)
    r = discover(search_config={"queries": ["x"]}, token="t")
    assert asked == ["4300000302"] and r["stats"]["details_fallback"] == 1 and r["stats"]["eligible"] == 2
    fb = next(j for j in r["eligible"] if j["ref_id"] == "4300000302")
    assert fb["seniority_level"] == "Entry level" and fb["company_industry"] == "Software Development"
    assert "Build pipelines" in fb["description"].replace("\xa0", " ") and "- SQL" in fb["description"]
    assert fb["link"] == "https://www.linkedin.com/jobs/view/4300000302"


def test_closed_job_state_counts_as_expired():
    from datetime import datetime, timezone

    from scrape_jobs import normalize
    j = normalize({"id": "4300000401", "title": "T", "companyName": "C",
                   "link": "https://www.linkedin.com/jobs/view/4300000401", "jobState": "CLOSED"},
                  datetime.now(timezone.utc))
    assert j["job_status"] == "expired"


def test_run_capped_by_spend_keeps_partial_results(monkeypatch):
    import scrape_jobs

    class Resp:
        def __init__(self, data):
            self._d = data

        def json(self):
            return self._d
    calls = []

    def fake_request(method, url, token, **kw):
        calls.append(url)
        if url.endswith("/runs"):
            return Resp({"data": {"id": "R1", "status": "ABORTED", "defaultDatasetId": "D1",
                                  "statusMessage": "This run was aborted because it reached its maximum cost of $0.15."}})
        return Resp([{"jobId": "4300000501"}])
    monkeypatch.setattr(scrape_jobs, "_request", fake_request)
    assert scrape_jobs.run_actor("A", {}, "t", 10, 0.15) == [{"jobId": "4300000501"}]


def test_run_aborted_for_other_reasons_still_fails(monkeypatch):
    import pytest

    import scrape_jobs

    class Resp:
        def json(self):
            return {"data": {"id": "R1", "status": "FAILED", "defaultDatasetId": "D1", "statusMessage": "crash"}}
    monkeypatch.setattr(scrape_jobs, "_request", lambda *a, **k: Resp())
    with pytest.raises(scrape_jobs.ScrapeError):
        scrape_jobs.run_actor("A", {}, "t", 10, 0.15)


def test_reused_search_hits_skip_the_search_actor(monkeypatch):
    import scrape_jobs
    monkeypatch.setattr(scrape_jobs, "search_job_ids", lambda cfg, token: (_ for _ in ()).throw(AssertionError("searched")))
    monkeypatch.setattr(scrape_jobs, "fetch_details", lambda ids, token, cap: [])
    monkeypatch.setattr(scrape_jobs, "fetch_details_fallback", lambda ids, token, cap=0.05: [])
    s = discover(search_config={"queries": ["x"]}, token="t",
                 search_hits_override=[{"jobId": "4300000601", "title": "Data Analyst"}, {"jobId": None}])["stats"]
    assert s["search_hits"] == 1 and s["details_requested"] == 1


def test_primary_detail_actor_crash_falls_back_for_all(monkeypatch):
    import scrape_jobs
    monkeypatch.setattr(scrape_jobs, "search_job_ids", lambda cfg, token: [{"jobId": "4300000701", "title": "Data Analyst"}])

    def boom(ids, token, cap):
        raise scrape_jobs.ScrapeError("Apify run X ended with status FAILED")
    monkeypatch.setattr(scrape_jobs, "fetch_details", boom)
    monkeypatch.setattr(scrape_jobs, "fetch_details_fallback", lambda ids, token, cap=0.05: [
        {"id": i, "title": "Data Analyst", "companyName": "A", "link": f"https://www.linkedin.com/jobs/view/{i}"} for i in ids])
    s = discover(search_config={"queries": ["x"]}, token="t")["stats"]
    assert s["details_fallback"] == 1 and s["eligible"] == 1
