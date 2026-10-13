"""End-to-end pipeline runs with fake Apify data and a fake LLM; everything writes to a temp workspace."""

import json
from pathlib import Path

import pytest

import run_pipeline as rp
from excel_store import JobStore
from openrouter import DailyLimitReached
from test_tailoring import tailored_plan
from workspace import get_workspace

LONG_DESC = ("We are hiring an entry-level data engineer to build PySpark and SQL pipelines on Databricks, "
             "clean and validate datasets, and support analytics dashboards. Freshers and 2026 graduates are "
             "welcome. You will work with senior engineers, learn Delta Lake and AWS S3, and document your work. "
             "Competitive stipend and full-time conversion for strong performers.")
RAW = [({**i, "descriptionText": LONG_DESC, "companyLinkedinUrl": "https://www.linkedin.com/company/acme"}
        if isinstance(i, dict) else i)
       for i in json.loads((Path(__file__).parent / "fixtures" / "apify_linkedin_sample.json").read_text(encoding="utf-8"))]
GOOD_ID = "4100000001"   # Junior Data Engineer @ Acme (open)
AI_ID = "4100000003"     # AI Engineer @ Gamma (no ref id field, ID taken from URL)


def evaluation(score_parts=(9, 9, 9, 9), years=0, missing=("Airflow",)):
    s, e, p, r = score_parts
    return {"skills_match": s, "experience_fit": e, "project_relevance": p, "role_alignment": r,
            "min_years_required": years,
            "matched_skills": [{"skill": "PySpark", "resume_evidence": "PySpark, Databricks, Delta Lake"},
                               {"skill": "SQL", "resume_evidence": "Python, SQL"},
                               {"skill": "Databricks", "resume_evidence": "PySpark, Databricks"}],
            "missing_skills": list(missing), "required_skills": ["PySpark", "SQL"], "preferred_skills": ["Airflow"],
            "experience_match": "", "project_match": "", "location_match": "", "salary_match": "", "reasoning": "ok"}


class FakeLLM:
    """Routes by tool name: scoring returns queued evaluations, tailoring returns queued plans."""

    def __init__(self, evaluations=(), plans=()):
        self.evaluations, self.plans = list(evaluations), list(plans)
        self.model, self.calls = "fake", {"submit_evaluation": 0, "submit_tailoring_plan": 0}

    def factory(self, budget):
        return self

    def call_tool(self, messages, tool, temperature=0):
        name = tool["function"]["name"]
        self.calls[name] += 1
        queue = self.evaluations if name == "submit_evaluation" else self.plans
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def run(llm, **kw):
    ws = get_workspace()
    return ws, rp.run(ws, run_id=kw.pop("run_id", "run1"), client_factory=llm.factory,
                      search_config={}, **{"raw_items": RAW, "tailor": True, **kw})


def rows(ws):
    return {r["Job ID"]: r for r in JobStore(ws.jobs_xlsx).all()}


def test_full_run_scores_tailors_and_records_paths():
    # fixture -> 2 eligible jobs (Acme, Gamma) + 1 expired (Beta)
    llm = FakeLLM(evaluations=[evaluation(), evaluation((3, 3, 3, 3))], plans=[tailored_plan()])
    ws, stats = run(llm)
    data = rows(ws)
    assert stats["rows_before"] == 0 and stats["rows_after"] == 3
    assert stats["qualified"] == 1 and stats["rejected_low_fit"] == 1 and stats["resumes_generated"] == 1

    good = next(r for r in data.values() if r["Source Reference ID"] == GOOD_ID)
    assert good["Processing Status"] == "ready_for_review" and good["Match Score"] == 8.6   # decimal, not rounded
    assert Path(good["Resume Path"]).is_absolute() and Path(good["Resume Path"]).is_file()
    assert Path(good["Tailoring Report Path"]).is_file()
    assert Path(good["Resume Path"]).parent.name == good["Job ID"]
    assert good["Required Skills"] == "PySpark; SQL" and good["Preferred Skills"] == "Airflow"
    assert good["Application Status"] == "Not Applied"

    low = next(r for r in data.values() if r["Source Reference ID"] == AI_ID)
    assert low["Processing Status"] == "rejected_low_fit" and low["Resume Path"] is None
    assert not (ws.resumes_dir / low["Job ID"]).exists()                 # below 8: never tailored
    expired = [r for r in data.values() if r["Processing Status"] == "expired"]
    assert len(expired) == 1 and expired[0]["Match Score"] is None
    assert (ws.logs_dir / "run1_summary.json").is_file()


def test_rerun_creates_no_duplicates_and_keeps_resumes():
    llm = FakeLLM(evaluations=[evaluation(), evaluation((3, 3, 3, 3))], plans=[tailored_plan()])
    ws, _ = run(llm)
    resume = Path(next(r for r in rows(ws).values() if r["Resume Path"])["Resume Path"])
    before = resume.read_text(encoding="utf-8")

    llm2 = FakeLLM()   # no queued responses: any LLM call would fail the test
    _, stats = run(llm2, run_id="run2")
    assert stats["rows_inserted"] == 0 and stats["rows_after"] == 3
    assert stats["discovery"]["previously_seen"] == 3 and stats["discovery"]["eligible"] == 0
    assert llm2.calls == {"submit_evaluation": 0, "submit_tailoring_plan": 0}
    assert resume.read_text(encoding="utf-8") == before


def test_failed_generation_recorded_and_retried_next_run():
    bad = tailored_plan()
    bad["summary"] += " Experienced with Airflow."
    llm = FakeLLM(evaluations=[evaluation(), evaluation((3, 3, 3, 3))], plans=[bad, bad])
    ws, stats = run(llm)
    good = next(r for r in rows(ws).values() if r["Source Reference ID"] == GOOD_ID)
    assert stats["validation_failed"] == 1 and good["Processing Status"] == "validation_failed"
    assert "Airflow" in good["Error Details"] and good["Resume Path"] is None
    assert not (ws.resumes_dir / good["Job ID"]).exists()

    llm2 = FakeLLM(plans=[tailored_plan()])
    _, stats2 = run(llm2, run_id="run2", skip_discovery=True)
    good = rows(ws)[good["Job ID"]]
    assert stats2["resumes_generated"] == 1 and good["Processing Status"] == "ready_for_review"
    assert good["Error Details"] is None and Path(good["Resume Path"]).is_file()
    assert llm2.calls["submit_evaluation"] == 0                          # not re-scored


def test_threshold_boundary_8_tailored_7_not():
    llm = FakeLLM(evaluations=[evaluation((8, 8, 8, 8)), evaluation((7, 8, 7, 7))], plans=[tailored_plan()])
    ws, stats = run(llm)
    scores = {r["Source Reference ID"]: r for r in rows(ws).values()}
    assert scores[GOOD_ID]["Match Score"] == 8 and scores[GOOD_ID]["Processing Status"] == "ready_for_review"
    assert scores[AI_ID]["Match Score"] == 7.25 and scores[AI_ID]["Processing Status"] == "rejected_low_fit"
    assert llm.calls["submit_tailoring_plan"] == 1


def test_experience_cap_blocks_tailoring_even_with_perfect_subscores():
    llm = FakeLLM(evaluations=[evaluation((10, 10, 10, 10), years=3), evaluation((3, 3, 3, 3))])
    ws, stats = run(llm)
    assert stats["qualified"] == 0 and llm.calls["submit_tailoring_plan"] == 0


def test_daily_limit_leaves_jobs_retryable():
    llm = FakeLLM(evaluations=[DailyLimitReached("limit")])
    ws, stats = run(llm)
    assert stats["unscored"] == 2
    assert {r["Processing Status"] for r in rows(ws).values()} == {"unscored", "expired"}

    llm2 = FakeLLM(evaluations=[evaluation(), evaluation((3, 3, 3, 3))], plans=[tailored_plan()])
    _, stats2 = run(llm2, run_id="run2", skip_discovery=True)
    assert stats2["evaluated"] == 2 and stats2["resumes_generated"] == 1


def test_deleted_resume_is_regenerated():
    llm = FakeLLM(evaluations=[evaluation(), evaluation((3, 3, 3, 3))], plans=[tailored_plan()])
    ws, _ = run(llm)
    resume = Path(next(r for r in rows(ws).values() if r["Resume Path"])["Resume Path"])
    resume.unlink()
    llm2 = FakeLLM(plans=[tailored_plan()])
    _, stats = run(llm2, run_id="run2", skip_discovery=True)
    assert stats["resumes_generated"] == 1 and resume.is_file()


def test_user_application_status_survives_reruns():
    llm = FakeLLM(evaluations=[evaluation(), evaluation((3, 3, 3, 3))], plans=[tailored_plan()])
    ws, _ = run(llm)
    jid = next(r["Job ID"] for r in rows(ws).values() if r["Resume Path"])
    store = JobStore(ws.jobs_xlsx)
    store.ws.cell(row=store._rows[jid], column=store.col["Application Status"], value="Applied")
    store.save()
    run(FakeLLM(plans=[tailored_plan()]), run_id="run2", skip_discovery=True, job_ids=[jid], force=True)
    assert rows(ws)[jid]["Application Status"] == "Applied"


def test_dry_run_writes_nothing(capsys):
    llm = FakeLLM(evaluations=[evaluation(), evaluation((3, 3, 3, 3))], plans=[tailored_plan()])
    ws, stats = run(llm, dry_run=True)
    assert not ws.jobs_xlsx.exists() and not any(ws.resumes_dir.iterdir())
    assert stats["qualified"] == 1 and "# Sample Candidate" in capsys.readouterr().out


def test_no_qualifying_jobs_finishes_gracefully():
    llm = FakeLLM(evaluations=[evaluation((3, 3, 3, 3)), evaluation((2, 2, 2, 2))])
    ws, stats = run(llm)
    assert stats["qualified"] == 0 and stats["resumes_generated"] == 0 and not stats["errors"]


@pytest.mark.parametrize("value,expected", [("Remote", "remote"), ("Hybrid", "hybrid"), ("On-site", "onsite"),
                                            ("Onsite", "onsite"), (None, None), ("Flexible", None)])
def test_work_mode_normalized(value, expected):
    assert rp._work_mode(value) == expected


def test_cli_overrides_read_seeded_config_on_first_run(monkeypatch):
    seen = {}

    def fake_run(ws, **kw):
        seen.update(kw)
        return {"errors": []}

    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(rp, "run", fake_run)
    assert rp.main(["--max-search-results", "5", "--dry-run"]) == 0
    assert seen["search_config"]["max_search_results"] == 5 and seen["search_config"]["queries"]


def test_linkedin_seniority_excluded_before_scoring():
    """Mid-Senior can never pass the strict rule: excluded without an LLM call (seniority isn't an Excel column)."""
    raw = [{**RAW[0], "seniorityLevel": "Mid-Senior level"}]
    llm = FakeLLM()
    ws, stats = run(llm, raw_items=raw)
    assert stats["excluded_experience"] == 1 and llm.calls["submit_evaluation"] == 0
    row = next(iter(rows(ws).values()))
    assert row["Processing Status"] == "excluded_experience" and "Mid-Senior" in row["Error Details"]


def test_seniority_recovered_from_snapshot_on_later_retry():
    raw = [{**RAW[0], "seniorityLevel": "Associate"}, {**RAW[2], "seniorityLevel": "Director"}]
    ws, stats = run(FakeLLM(evaluations=[DailyLimitReached("limit")]), raw_items=raw)
    assert stats["unscored"] == 1 and stats["excluded_experience"] == 1
    llm2 = FakeLLM(evaluations=[evaluation((10, 10, 10, 10), years=2)])
    _, stats2 = run(llm2, run_id="run2", skip_discovery=True)
    assert stats2["evaluated"] == 1 and stats2["qualified"] == 0     # model-reported years still capped


def test_years_in_description_excluded_before_scoring():
    raw = [{**RAW[0], "descriptionText": LONG_DESC + " Requires 3+ years of experience with Spark."}]
    llm = FakeLLM()
    _, stats = run(llm, raw_items=raw)
    assert stats["excluded_experience"] == 1 and llm.calls["submit_evaluation"] == 0


def test_precomputed_agent_evaluations_use_code_guards():
    from agent_scoring import PrecomputedClient
    ws = get_workspace()
    rp.run(ws, run_id="r0", client_factory=FakeLLM(evaluations=[DailyLimitReached("x")]).factory,
           search_config={}, raw_items=RAW)                       # 2 jobs land as unscored
    ids = [r["Job ID"] for r in rows(ws).values() if r["Processing Status"] == "unscored"]
    by_ref = {r["Job ID"]: r["Source Reference ID"] for r in rows(ws).values()}
    evals = {i: (top_eval() if by_ref[i] == GOOD_ID else evaluation((10, 10, 10, 10), years=2)) for i in ids}
    client = PrecomputedClient(evals)
    stats = rp.run(ws, run_id="r1", client_factory=lambda b: client, skip_discovery=True, job_ids=ids,
                   open_checker=lambda refs: {r: (True, "open") for r in refs})
    assert stats["verified_top_matches_total"] == 1 and stats["evaluated"] == 2
    statuses = {by_ref[k]: v["Processing Status"] for k, v in rows(ws).items()}
    assert statuses[GOOD_ID] == "verified_top_match" and statuses[AI_ID] == "rejected_low_fit"



# ---- this phase: screening, > 9.0 top matches, open verification, tailoring paused

def top_eval():
    return evaluation((10, 10, 10, 10), missing=())


def test_tailoring_paused_by_default_creates_no_resume_files():
    llm = FakeLLM(evaluations=[evaluation(), top_eval()])
    ws, stats = run(llm, tailor=False, open_checker=lambda ids: {i: (True, "open") for i in ids})
    assert stats["tailoring"] == "paused" and llm.calls["submit_tailoring_plan"] == 0
    assert not any(ws.resumes_dir.iterdir())
    assert all(r["Resume Path"] is None and r["Tailoring Report Path"] is None for r in rows(ws).values())


def test_top_match_above_9_verified_open_and_decimal_score_stored():
    llm = FakeLLM(evaluations=[top_eval(), evaluation((3, 3, 3, 3))])
    checked = []

    def checker(ids):
        checked.extend(ids)
        return {i: (True, "open on LinkedIn") for i in ids}
    ws, stats = run(llm, tailor=False, open_checker=checker)
    top = next(r for r in rows(ws).values() if r["Source Reference ID"] == GOOD_ID)
    assert top["Match Score"] == 10.0 and top["Processing Status"] == "verified_top_match"
    assert checked == [GOOD_ID] and stats["verified_open"] == 1 and stats["verified_top_matches_total"] == 1


def test_exactly_9_point_0_is_not_a_top_match():
    # 9*.4 + 9*.25 + 9*.2 + 9*.15 = 9.0 -> not strictly greater
    llm = FakeLLM(evaluations=[evaluation((9, 9, 9, 9), missing=()), evaluation((3, 3, 3, 3))])
    ws, stats = run(llm, tailor=False, open_checker=lambda ids: {i: (True, "open") for i in ids})
    row = next(r for r in rows(ws).values() if r["Source Reference ID"] == GOOD_ID)
    assert row["Match Score"] == 9.0 and row["Processing Status"] == "qualified"
    assert stats["top_matches_new"] == 0


def test_closed_and_unconfirmed_top_matches_not_verified():
    llm = FakeLLM(evaluations=[top_eval(), top_eval()])
    states = {GOOD_ID: (False, "closed"), AI_ID: (None, "not returned")}
    ws, stats = run(llm, tailor=False, open_checker=lambda ids: {i: states[i] for i in ids})
    by_ref = {r["Source Reference ID"]: r for r in rows(ws).values()}
    assert by_ref[GOOD_ID]["Processing Status"] == "closed"
    assert by_ref[AI_ID]["Processing Status"] == "top_match_unverified"
    assert stats["verified_top_matches_total"] == 0


def test_verified_match_rechecked_and_closed_on_later_run():
    ws, _ = run(FakeLLM(evaluations=[top_eval(), evaluation((3, 3, 3, 3))]), tailor=False,
                open_checker=lambda ids: {i: (True, "open") for i in ids})
    _, stats = run(FakeLLM(), run_id="run2", skip_discovery=True, tailor=False,
                   open_checker=lambda ids: {i: (False, "now closed") for i in ids})
    row = next(r for r in rows(ws).values() if r["Source Reference ID"] == GOOD_ID)
    assert row["Processing Status"] == "closed" and stats["closed"] == 1


def test_staffing_and_red_flag_jobs_excluded_before_scoring():
    raw = [{**RAW[0], "companyName": "OrbiTouch HR (orbiTouch outsourcing pvt ltd)"},
           {**RAW[2], "descriptionText": "Data analyst. Apply now."},
           {**RAW[3], "id": "4100000009", "link": "https://www.linkedin.com/jobs/view/4100000009",
            "descriptionText": LONG_DESC + " A refundable security deposit of Rs 5000 is required."}]
    llm = FakeLLM()
    ws, stats = run(llm, raw_items=raw, tailor=False)
    statuses = sorted(r["Processing Status"] for r in rows(ws).values())
    assert statuses == ["excluded_red_flag", "excluded_red_flag", "excluded_staffing"]
    assert llm.calls["submit_evaluation"] == 0
    assert all(r["Error Details"] for r in rows(ws).values())


def test_job_without_company_page_is_red_flag():
    raw = [{**RAW[0], "companyLinkedinUrl": None}]
    ws, stats = run(FakeLLM(), raw_items=raw, tailor=False)
    assert stats["excluded_red_flag"] == 1
