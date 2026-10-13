import pytest

from conftest import FIXTURE_RESUME
from resume_parser import load_base_resume
from tailor_job import apply_plan
from validate_resume import validate_resume

BASE = load_base_resume(FIXTURE_RESUME)
META = {"job_id": "c1b8d0123359b303", "ref_id": "4467378631", "job_url": "https://www.linkedin.com/jobs/view/4467378631"}


def identity_plan():
    return {"summary": BASE.summary,
            "skill_groups": [{"group_id": g.id, "items": g.items} for g in BASE.skills],
            "experience": [{"entry_id": e.id, "bullets": [{"source_id": b.id, "text": b.text} for b in e.bullets]}
                           for e in BASE.experience],
            "projects": [{"entry_id": p.id, "bullets": [{"source_id": b.id, "text": b.text} for b in p.bullets]}
                         for p in BASE.projects]}


GOOD, _ = apply_plan(BASE, identity_plan())


def errors(md, meta=META, gaps=None):
    return validate_resume(md, BASE, meta, gaps).errors


def test_good_resume_passes():
    assert validate_resume(GOOD, BASE, META, ["Azure services", "CI/CD"]).passed


# ---- structure / placeholders / truncation

def test_empty_and_truncated_fail():
    assert errors("")
    assert errors(GOOD[: len(GOOD) // 3])


@pytest.mark.parametrize("placeholder", ["[Your Name]", "TODO", "INSERT HERE", "[Company Name]", "{{skills}}"])
def test_placeholders_fail(placeholder):
    assert any("placeholder" in e for e in errors(GOOD.replace("Hands-on project", f"{placeholder} Hands-on project")))


def test_missing_section_fails():
    md = GOOD.replace("## Education", "## Schooling")
    assert any("missing section: Education" in e for e in errors(md))


def test_wrong_name_or_contact_fails():
    assert errors(GOOD.replace("# Sample Candidate", "# Someone Else"))
    assert any("contact" in e for e in errors(GOOD.replace("90000 12345", "90000 00000")))


# ---- fabrication

def test_added_tool_from_job_description_fails():
    md = GOOD.replace("PySpark, Databricks, Delta Lake, ETL/ELT", "PySpark, Databricks, Delta Lake, Azure, ETL/ELT")
    errs = errors(md)
    assert any("Azure" in e for e in errs)


def test_unsupported_term_in_bullet_fails():
    md = GOOD.replace("Built Power BI dashboards", "Built Power BI and Tableau dashboards")
    assert any("Tableau" in e for e in errors(md))


def test_gap_term_from_scoring_fails():
    md = GOOD.replace("Hands-on project experience", "Hands-on project experience with CI/CD fundamentals and")
    assert any("CI/CD" in e for e in errors(md, gaps=["CI/CD"]))


def test_invented_metric_fails():
    md = GOOD.replace("prepared a cleaned dataset", "improved accuracy by 35% and prepared a cleaned dataset")
    assert any("'35'" in e for e in errors(md))


def test_years_of_experience_claim_fails():
    md = GOOD.replace("Computer Science graduate", "Data engineer with 2+ years of experience and Computer Science graduate")
    assert any("experience-length" in e for e in errors(md))


def test_seniority_inflation_fails():
    assert any("expert" in e for e in errors(GOOD.replace("Hands-on project", "Expert in hands-on project")))


def test_changed_dates_fail():
    assert any("dates changed" in e for e in errors(GOOD.replace("Jun 2026 – Oct 2026", "Jun 2025 – Oct 2026")))


def test_changed_employer_fails():
    assert errors(GOOD.replace("### Example Analytics — Engineering Intern", "### Example Analytics — Data Engineer"))


def test_invented_project_fails():
    md = GOOD.replace("### Customer Shopping Analytics", "### Real-time Fraud Detection System")
    assert any("project not in base resume" in e for e in errors(md))


def test_changed_project_technologies_fail():
    md = GOOD.replace("**Technologies:** Python, Pandas, scikit-learn, Power BI",
                      "**Technologies:** Python, Pandas, scikit-learn, Power BI, Airflow")
    assert any("technologies line changed" in e for e in errors(md))


def test_added_certification_fails():
    md = GOOD.replace("- Python Programming — Code Tantra",
                      "- Python Programming — Code Tantra\n- Microsoft Certified: Fabric Data Engineer (DP-700)")
    assert any("certifications" in e for e in errors(md))


def test_changed_cgpa_fails():
    assert any("education" in e for e in errors(GOOD.replace("CGPA: 8.4", "CGPA: 9.4")))


def test_skill_moved_into_wrong_group_or_invented_fails():
    assert errors(GOOD.replace("- **Programming:** Java, Data Structures & Algorithms",
                               "- **Programming:** Java, Scala, Data Structures & Algorithms"))


def test_ordinary_english_word_not_flagged_as_tool():
    md = GOOD.replace("Hands-on project experience", "Eager to excel and go deeper, with hands-on project experience")
    assert validate_resume(md, BASE, META).passed


# ---- metadata

def test_metadata_required():
    assert any("job_url" in e for e in errors(GOOD, meta={"job_id": "x", "ref_id": None}))
    assert validate_resume(GOOD, BASE, {**META, "ref_id": None}).passed   # null ref_id is fine


# ---- Claude Code hook

def _run_hook(event):
    import subprocess
    import sys as _sys
    from pathlib import Path as _P
    hook = _P(__file__).resolve().parent.parent / ".claude" / "hooks" / "resume_hook.py"
    return subprocess.run([_sys.executable, str(hook)], input=json.dumps(event), capture_output=True,
                          text=True, encoding="utf-8")


import json  # noqa: E402


def _resume_path(ws_root, job_id="c1b8d0123359b303"):
    d = ws_root / "output" / "resumes" / job_id
    d.mkdir(parents=True, exist_ok=True)
    return d / "resume.md"


def test_hook_blocks_fabricated_write(isolated_workspace):
    bad = GOOD.replace("Built Power BI dashboards", "Built Tableau dashboards")
    p = _run_hook({"hook_event_name": "PreToolUse", "tool_name": "Write",
                   "tool_input": {"file_path": str(_resume_path(isolated_workspace)), "content": bad}})
    assert p.returncode == 2 and "Tableau" in p.stderr


def test_hook_allows_valid_write_and_ignores_other_files(isolated_workspace):
    ok = _run_hook({"hook_event_name": "PreToolUse", "tool_name": "Write",
                    "tool_input": {"file_path": str(_resume_path(isolated_workspace)), "content": GOOD}})
    base = _run_hook({"hook_event_name": "PreToolUse", "tool_name": "Write",
                      "tool_input": {"file_path": str(isolated_workspace / "resume.md"), "content": "TODO"}})
    assert ok.returncode == 0 and base.returncode == 0


def test_hook_uses_missing_skills_from_jobs_xlsx(isolated_workspace):
    from excel_store import JobStore
    store = JobStore(isolated_workspace / "jobs.xlsx")
    store.upsert("c1b8d0123359b303", {"Job URL": META["job_url"], "Missing Skills": ["data contracts"],
                                      "Processing Status": "ready_for_review"})
    store.save()
    path = _resume_path(isolated_workspace)
    path.write_text(GOOD.replace("Hands-on project experience", "Hands-on project experience with data contracts and"),
                    encoding="utf-8")
    p = _run_hook({"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": {"file_path": str(path)}})
    assert p.returncode == 2 and "data contracts" in p.stderr
    assert JobStore(isolated_workspace / "jobs.xlsx").get("c1b8d0123359b303")["Processing Status"] == "validation_failed"
