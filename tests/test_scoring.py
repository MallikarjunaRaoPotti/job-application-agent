from pathlib import Path

import pytest

import score_jobs as sj

RESUME = (Path(__file__).resolve().parent / "fixtures" / "resume.md").read_text(encoding="utf-8")
RESUME_NORM = sj._norm(RESUME)
JOB = {"job_id": "abc123", "ref_id": "4100000001", "title": "Junior Data Engineer", "company": "Acme",
       "description": "Build PySpark pipelines.", "experience_required": None}


def raw(skills=9, exp=9, proj=8, role=9, years=0, matched=None, missing=None):
    return {"skills_match": skills, "experience_fit": exp, "project_relevance": proj, "role_alignment": role,
            "min_years_required": years,
            "matched_skills": matched if matched is not None else [
                {"skill": "PySpark", "resume_evidence": "PySpark, Databricks, Delta Lake"}],
            "missing_skills": missing or [], "experience_match": "", "project_match": "",
            "location_match": "", "salary_match": "", "reasoning": ""}


class FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)

    def call_tool(self, messages, tool):
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


# ---- weights, rounding, cutoff

def test_weighted_score_and_qualification():
    ev = sj.compute_evaluation(JOB, raw(), RESUME_NORM)
    # 9*.4 + 9*.25 + 8*.2 + 9*.15 = 8.8 -> 9
    assert ev["weighted_score"] == 8.8 and ev["fit_score"] == 9
    assert ev["qualification_status"] == "qualified" and ev["status"] == "qualified"
    assert ev["job_id"] == "abc123" and ev["ref_id"] == "4100000001"


def test_exactly_8_qualifies_and_7_does_not():
    assert sj.compute_evaluation(JOB, raw(8, 8, 8, 8), RESUME_NORM)["fit_score"] == 8
    assert sj.compute_evaluation(JOB, raw(8, 8, 8, 8), RESUME_NORM)["qualification_status"] == "qualified"
    ev = sj.compute_evaluation(JOB, raw(7, 8, 7, 7), RESUME_NORM)   # 7.25 -> 7
    assert ev["fit_score"] == 7 and ev["status"] == "rejected_low_fit"
    assert any("below 8" in r for r in ev["rejection_reasons"])


def test_score_clamped_to_1_10():
    assert sj.compute_evaluation(JOB, raw(0, 0, 0, 0, matched=[]), RESUME_NORM)["fit_score"] == 1
    assert sj.compute_evaluation(JOB, raw(10, 10, 10, 10), RESUME_NORM)["fit_score"] == 10


# ---- strict experience cap

@pytest.mark.parametrize("years,qualifies", [(0, True), (1, True), (1.5, False), (2, False), (5, False)])
def test_experience_cap(years, qualifies):
    ev = sj.compute_evaluation(JOB, raw(10, 10, 10, 10, years=years), RESUME_NORM)
    assert (ev["qualification_status"] == "qualified") is qualifies
    if not qualifies:
        assert ev["fit_score"] == sj.EXPERIENCE_CAP_SCORE
        assert any("years of experience" in r for r in ev["rejection_reasons"])


def test_experience_falls_back_to_structured_field():
    job = {**JOB, "experience_required": "3-5 years"}
    ev = sj.compute_evaluation(job, raw(10, 10, 10, 10, years=None), RESUME_NORM)
    assert ev["min_years_required"] == 3 and ev["min_years_source"] == "experience_required"
    assert ev["qualification_status"] == "rejected"


@pytest.mark.parametrize("text,expected", [("2-4 years", 2), ("3+ yrs", 3), ("Fresher", 0),
                                           ("0-1 years", 0), ("1 year", 1), (None, None), ("N/A", None)])
def test_years_from_text(text, expected):
    assert sj._years_from_text(text) == expected


# ---- no hallucinated skills

def test_unverified_matched_skills_moved_to_missing_and_score_reduced():
    matched = [{"skill": "PySpark", "resume_evidence": "PySpark, Databricks, Delta Lake"},
               {"skill": "Kubernetes", "resume_evidence": "Deployed Kubernetes clusters"}]
    ev = sj.compute_evaluation(JOB, raw(10, 10, 10, 10, matched=matched, missing=["Airflow"]), RESUME_NORM)
    assert ev["matched_skills"] == ["PySpark"]
    assert "Kubernetes" in ev["missing_skills"] and "Airflow" in ev["missing_skills"]
    assert ev["unverified_claims"] == ["Kubernetes"]
    assert ev["sub_scores"]["skills_match"] == 4   # 10 * 1/2 unverified, then ratio cap ceil(10 * 1/3)


def test_evidence_match_ignores_markdown_and_case():
    assert sj.evidence_in_resume("technologies: python, sql, claude", RESUME_NORM)
    assert not sj.evidence_in_resume("", RESUME_NORM)


def test_malformed_subscore_raises():
    bad = raw()
    bad["skills_match"] = "high"
    with pytest.raises(sj.ScoringError):
        sj.compute_evaluation(JOB, bad, RESUME_NORM)


# ---- privacy

def test_redact_contact_strips_phone_email_links():
    red = sj.redact_contact(RESUME)
    assert "@" not in red and "90000 12345" not in red and "linkedin.com" not in red and "github.com" not in red
    assert "Sample Candidate" in red and "PySpark" in red and "99,957" in red


# ---- response parsing

def test_parse_tool_call():
    data = {"choices": [{"message": {"tool_calls": [{"function": {"name": "submit_evaluation",
                                                                "arguments": '{"skills_match": 5}'}}]}}]}
    assert sj.parse_tool_response(data) == {"skills_match": 5}


def test_parse_falls_back_to_json_in_content():
    data = {"choices": [{"message": {"content": 'Here: {"skills_match": 4}'}}]}
    assert sj.parse_tool_response(data) == {"skills_match": 4}


def test_parse_rejects_garbage():
    with pytest.raises(sj.ScoringError):
        sj.parse_tool_response({"choices": [{"message": {"content": "no json"}}]})


# ---- pipeline behaviour

def test_pipeline_gate_retry_and_daily_limit():
    jobs = [{**JOB, "job_id": f"j{i}", "ref_id": None} for i in range(4)]
    client = FakeClient([
        raw(),                                   # j0 qualified
        raw(3, 3, 3, 3, matched=[]),             # j1 rejected
        sj.ScoringError("bad json"), raw(),      # j2 retried once, then qualified
        sj.DailyLimitReached("limit"),           # j3 left unscored
    ])
    res = sj.score_jobs(jobs, client, RESUME, "run1")
    assert [j["job_id"] for j in res["qualified"]] == ["j0", "j2"]
    assert [j["job_id"] for j in res["rejected"]] == ["j1"]
    assert [j["job_id"] for j in res["unscored"]] == ["j3"]
    assert all(j["evaluation"]["fit_score"] >= 8 for j in res["qualified"])
    assert res["stats"]["evaluated"] == 3
    assert res["qualified"][0]["evaluation"]["ref_id"] is None   # missing ref_id doesn't crash


def test_scoring_error_after_retry_is_recorded_not_rejected():
    res = sj.score_jobs([JOB], FakeClient([sj.ScoringError("x"), sj.ScoringError("y")]), RESUME, "run1")
    assert res["stats"]["scoring_errors"] == 1 and res["stats"]["rejected_low_fit"] == 0


def test_zero_jobs_finishes_gracefully():
    res = sj.score_jobs([], FakeClient([]), RESUME, "run1")
    assert res["stats"]["evaluated"] == 0 and res["qualified"] == []


# ---- code-side guards against a lenient model

@pytest.mark.parametrize("desc,expected", [
    ("We need 1 to 3 years of relevant experience in data engineering.", 1),
    ("Minimum 3+ years experience with Spark. 5 years experience preferred.", 3),
    ("Experience: 2-4 yrs in SQL.", 2),
    ("Freshers welcome. Company founded 25 years ago.", None),
    (None, None),
])
def test_years_from_description(desc, expected):
    assert sj.years_from_description(desc) == expected


def test_description_years_override_lenient_model():
    job = {**JOB, "description": "Requires 2+ years of experience building pipelines."}
    ev = sj.compute_evaluation(job, raw(10, 10, 10, 10, years=None), RESUME_NORM)
    assert ev["min_years_required"] == 2 and ev["min_years_source"] == "description"
    assert ev["fit_score"] == sj.EXPERIENCE_CAP_SCORE and ev["qualification_status"] == "rejected"


@pytest.mark.parametrize("level,qualifies", [("Mid-Senior level", False), ("Director", False),
                                             ("Entry level", True), ("Not Applicable", True), (None, True)])
def test_linkedin_seniority_cap(level, qualifies):
    ev = sj.compute_evaluation({**JOB, "seniority_level": level}, raw(10, 10, 10, 10), RESUME_NORM)
    assert (ev["qualification_status"] == "qualified") is qualifies


def test_soft_skills_ignored_and_skills_capped_by_ratio():
    matched = [{"skill": "Python", "resume_evidence": "Python, SQL"},
               {"skill": "Communication", "resume_evidence": "communicate technical insights"}]
    ev = sj.compute_evaluation(JOB, raw(10, 10, 10, 10, matched=matched,
                                        missing=["Docker", "Kubernetes", "Strong analytical skills"]), RESUME_NORM)
    assert ev["matched_skills"] == ["Python"]
    assert ev["sub_scores"]["skills_match"] == 4   # ceil(10 * 1/3); soft-skill gap not counted


def test_machine_learning_is_not_a_soft_skill():
    assert not sj.SOFT_SKILL_RE.search("Machine Learning")
    assert not sj.SOFT_SKILL_RE.search("scikit-learn")
    assert sj.SOFT_SKILL_RE.search("Eager to learn") and sj.SOFT_SKILL_RE.search("Strong communication")
    matched = [{"skill": "scikit-learn", "resume_evidence": "scikit-learn, Prophet"}]
    ev = sj.compute_evaluation(JOB, raw(10, 10, 10, 10, matched=matched, missing=["Deep Learning"]), RESUME_NORM)
    assert ev["matched_skills"] == ["scikit-learn"] and ev["sub_scores"]["skills_match"] == 5
