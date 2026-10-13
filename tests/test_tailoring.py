import pytest

import tailor_job as tj
from conftest import FIXTURE_RESUME
from resume_parser import load_base_resume
from test_validation import identity_plan
from workspace import get_workspace

BASE = load_base_resume(FIXTURE_RESUME)
JOB = {"job_id": "c1b8d0123359b303", "ref_id": "4467378631", "source": "Apify", "title": "FS - Data Engineer - Associate",
       "company": "PwC Acceleration Center India", "link": "https://www.linkedin.com/jobs/view/4467378631",
       "description": "Build data pipelines with PySpark on Azure.",
       "evaluation": {"fit_score": 9, "qualification_status": "qualified", "matched_skills": ["PySpark"],
                      "missing_skills": ["Azure services", "CI/CD"], "reasoning": "Strong lakehouse match."}}


class FakeClient:
    model = "fake-model"

    def __init__(self, plans):
        self.plans, self.calls = list(plans), 0

    def call_tool(self, messages, tool, temperature=0):
        self.calls += 1
        p = self.plans.pop(0)
        if isinstance(p, Exception):
            raise p
        return p


def tailored_plan():
    plan = identity_plan()
    plan["summary"] = ("Computer Science graduate focused on Data Engineering, with hands-on project experience "
                       "building medallion-style pipelines using PySpark, Databricks, Delta Lake, and AWS S3.")
    plan["skill_groups"] = [{"group_id": "SK2", "items": ["PySpark", "Databricks", "Delta Lake"]},
                            {"group_id": "SK1", "items": ["SQL", "Python"]}]
    plan["projects"] = [plan["projects"][1], plan["projects"][0], plan["projects"][3]]
    plan["projects"][0]["bullets"][0]["text"] = ("Built a medallion-style PySpark pipeline to ingest and transform "
                                                 "e-commerce datasets across Bronze, Silver, and Gold layers.")
    plan["tailoring_notes"] = "Led with lakehouse work."
    return plan


@pytest.fixture
def ws():
    return get_workspace().setup()


# ---- apply_plan

def test_apply_plan_reorders_and_keeps_facts():
    md, issues = tj.apply_plan(BASE, tailored_plan())
    assert issues == []
    assert md.index("### E-Commerce Sales") < md.index("### Agentic AI Banking")
    assert "Telugu AI Voice Agent" not in md            # dropped project (3 kept)
    assert "- **Data Engineering:** PySpark, Databricks, Delta Lake" in md
    assert "Jun 2026 – Oct 2026" in md and "CGPA: 8.4" in md


def test_apply_plan_rejects_unknown_ids_and_invented_numbers():
    plan = tailored_plan()
    plan["projects"][0]["bullets"] = [{"source_id": "PROJ9.b1", "text": "Invented"},
                                      {"source_id": "PROJ2.b1", "text": "Built a pipeline processing 10 TB daily."}]
    _, issues = tj.apply_plan(BASE, plan)
    assert any("unknown source_id" in i for i in issues)
    assert any("introduces numbers" in i for i in issues)


def test_apply_plan_drops_invented_skills():
    plan = tailored_plan()
    plan["skill_groups"] = [{"group_id": "SK2", "items": ["PySpark", "Airflow"]}, {"group_id": "SK99", "items": ["Go"]}]
    md, _ = tj.apply_plan(BASE, plan)
    assert "Airflow" not in md and "SK99" not in md


def test_apply_plan_keeps_all_experience_bullets():
    plan = tailored_plan()
    plan["experience"] = []
    md, _ = tj.apply_plan(BASE, plan)
    assert all(b.text in md for b in BASE.experience[0].bullets)


def test_apply_plan_normalizes_nonbreaking_hyphens():
    plan = tailored_plan()
    plan["summary"] = plan["summary"].replace("hands-on", "hands‑on") + " "
    md, _ = tj.apply_plan(BASE, plan)
    assert "‑" not in md and " " not in md and "hands-on" in md


# ---- process_job

def test_process_job_writes_resume_and_report(ws):
    rec = tj.process_job(JOB, BASE, FakeClient([tailored_plan()]), ws, "run1")
    folder = ws.resumes_dir / JOB["job_id"]
    assert rec["status"] == "ready_for_review"
    assert rec["resume_path"] == str((folder / "resume.md").resolve())
    assert rec["report_path"] == str((folder / "tailoring_report.md").resolve())
    report = (folder / "tailoring_report.md").read_text(encoding="utf-8")
    assert "4467378631" in report and "PASSED" in report and "Azure services, CI/CD" in report
    assert "PROJ2.b1" in report                           # reworded bullet traced to its source
    assert (ws.resume.read_text(encoding="utf-8") == FIXTURE_RESUME.read_text(encoding="utf-8"))  # base untouched


def test_process_job_retries_then_passes(ws):
    bad = tailored_plan()
    bad["summary"] = bad["summary"] + " Experienced with Azure and CI/CD."
    client = FakeClient([bad, tailored_plan()])
    assert tj.process_job(JOB, BASE, client, ws, "run1")["status"] == "ready_for_review"
    assert client.calls == 2
    assert "2 attempts" in (ws.resumes_dir / JOB["job_id"] / "tailoring_report.md").read_text(encoding="utf-8")


def test_process_job_validation_failure_writes_no_resume(ws):
    bad = tailored_plan()
    bad["summary"] = bad["summary"] + " Experienced with Azure."
    rec = tj.process_job(JOB, BASE, FakeClient([bad, bad]), ws, "run1")
    assert rec["status"] == "validation_failed" and "Azure" in rec["error"]
    assert not (ws.resumes_dir / JOB["job_id"]).exists()
    assert (ws.logs_dir / "failed_resumes" / f"{JOB['job_id']}.md").is_file()


def test_process_job_refuses_below_cutoff(ws):
    job = {**JOB, "evaluation": {**JOB["evaluation"], "fit_score": 7}}
    client = FakeClient([])
    assert tj.process_job(job, BASE, client, ws, "run1")["status"] == "rejected_low_fit"
    assert client.calls == 0


def test_process_job_skips_existing_unless_forced(ws):
    tj.process_job(JOB, BASE, FakeClient([tailored_plan()]), ws, "run1")
    resume = ws.resumes_dir / JOB["job_id"] / "resume.md"
    resume.write_text(resume.read_text(encoding="utf-8") + "\n<!-- my edit -->\n", encoding="utf-8")
    client = FakeClient([tailored_plan()])
    rec = tj.process_job(JOB, BASE, client, ws, "run2")
    assert rec["skipped"] and client.calls == 0 and "my edit" in resume.read_text(encoding="utf-8")
    tj.process_job(JOB, BASE, client, ws, "run3", force=True)
    assert "my edit" not in resume.read_text(encoding="utf-8")
    assert len(list(ws.resumes_dir.iterdir())) == 1


def test_process_job_missing_ref_id(ws):
    rec = tj.process_job({**JOB, "ref_id": None}, BASE, FakeClient([tailored_plan()]), ws, "run1")
    assert rec["status"] == "ready_for_review"
    assert "none provided by source" in (ws.resumes_dir / JOB["job_id"] / "tailoring_report.md").read_text(encoding="utf-8")


def test_dry_run_writes_nothing(ws, capsys):
    rec = tj.process_job(JOB, BASE, FakeClient([tailored_plan()]), ws, "run1", dry_run=True)
    assert rec["status"] == "dry_run_passed" and not (ws.resumes_dir / JOB["job_id"]).exists()
    assert "# Sample Candidate" in capsys.readouterr().out


def test_unsafe_job_id_rejected(ws):
    with pytest.raises(ValueError):
        ws.job_dir("../../etc")
