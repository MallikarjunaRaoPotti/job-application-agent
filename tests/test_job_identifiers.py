from datetime import datetime, timezone

from scrape_jobs import canonical_url, extract_ref_id, make_job_id, normalize

NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


def test_job_id_is_deterministic_string():
    a = make_job_id("4100000001", "https://www.linkedin.com/jobs/view/4100000001")
    b = make_job_id("4100000001", "https://www.linkedin.com/jobs/view/4100000001?trk=x")
    assert a == b and isinstance(a, str) and len(a) == 16


def test_job_id_prefers_ref_id_over_url():
    assert make_job_id("4100000001", "https://a.com/x") == make_job_id("4100000001", "https://b.com/y")


def test_job_id_falls_back_to_canonical_url():
    a = make_job_id(None, "https://Careers.Example.com/jobs/1/?utm=x")
    b = make_job_id(None, "https://careers.example.com/jobs/1")
    assert a is not None and a == b


def test_job_id_none_without_any_identity():
    assert make_job_id(None, None) is None
    assert make_job_id(None, "not a url") is None


def test_ref_id_preserved_exactly_and_never_invented():
    assert extract_ref_id({"id": "4100000001"}) == "4100000001"
    assert extract_ref_id({"id": 4100000001}) == "4100000001"
    assert extract_ref_id({"id": None, "link": "https://www.linkedin.com/jobs/view/4100000003/"}) == "4100000003"
    assert extract_ref_id({"id": None, "link": "https://careers.example.com/jobs/1"}) is None


def test_canonical_linkedin_url_strips_slug_and_tracking():
    url = "https://in.linkedin.com/jobs/view/data-analyst-at-beta-corp-4100000002?trk=abc"
    assert canonical_url(url) == "https://www.linkedin.com/jobs/view/4100000002"


def test_normalize_schema_and_ids():
    job = normalize({"id": "4100000001", "title": "Junior Data Engineer", "companyName": "Acme",
                     "link": "https://www.linkedin.com/jobs/view/4100000001"}, NOW)
    assert job["ref_id"] == "4100000001"
    assert job["job_id"] == make_job_id("4100000001", None)
    assert job["source_record_id"] is None
    assert job["source"] == "Apify"
    assert job["salary"] is None and job["job_status"] == "open"


def test_normalize_rejects_malformed():
    assert normalize({"id": "1234567", "title": None, "companyName": "X"}, NOW) is None
    assert normalize("junk", NOW) is None


def test_expired_detection():
    job = normalize({"id": "4100000002", "title": "T", "companyName": "C",
                     "link": "https://www.linkedin.com/jobs/view/4100000002",
                     "applicationDeadline": "March 23, 2026"}, NOW)
    assert job["job_status"] == "expired"


def test_linkedin_signup_redirect_is_not_an_apply_url():
    base = {"id": "4467378631", "title": "T", "companyName": "C", "link": "https://www.linkedin.com/jobs/view/4467378631"}
    job = normalize({**base, "applyUrl": "https://www.linkedin.com/signup/cold-join?source=jobs_registration"}, NOW)
    assert job["apply_url"] is None
    job = normalize({**base, "applyUrl": "https://careers.example.com/apply/1"}, NOW)
    assert job["apply_url"] == "https://careers.example.com/apply/1"
