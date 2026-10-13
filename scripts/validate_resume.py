"""Validate a tailored resume against the base resume (<workspace>/resume.md) before it may count as complete.

Used by tailor_job.py (headless) and .claude/hooks/resume_hook.py (interactive). A resume that
fails here must not be saved as a final deliverable or recorded as successful.

Checks: structure, placeholders, truncation, verbatim fixed facts (name, contact, employers,
dates, education, certifications, project technologies), skills subset, numbers, unsupported
technical terms, job-description gap terms, experience-length claims, and job metadata.

Usage:
  python scripts/validate_resume.py <workspace>/output/resumes/<job_id>/resume.md
  (the job's jobs.xlsx row, found by the folder name, adds metadata + missing-skill checks)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from resume_parser import REQUIRED_SECTIONS, BaseResume, load_base_resume

MIN_CHARS, MAX_CHARS = 800, 15000
MIN_PROJECTS = 3

PLACEHOLDER_RE = re.compile(
    r"\[(?:your|insert|company|job|name|role|position|candidate)[^\]]*\]|\bTODO\b|\bTBD\b|INSERT HERE|"
    r"lorem ipsum|\bXXX\b|\{\{|\}\}|<[A-Z][A-Z_ ]{2,}>", re.I)
EXPERIENCE_CLAIM_RE = re.compile(r"\b\d+(?:\.\d+)?\s*\+?\s*(?:years?|yrs?)\b", re.I)
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")

# Technologies an LLM is tempted to borrow from a job description. Any of these appearing in a
# tailored resume but not in the base resume is an unsupported claim.
TECH_TERMS = [
    "Airflow", "Kafka", "Snowflake", "dbt", "Azure", "GCP", "Google Cloud", "BigQuery", "Redshift", "Glue",
    "Lambda", "EMR", "Athena", "Kinesis", "DynamoDB", "EC2", "SageMaker", "Bedrock", "Docker", "Kubernetes",
    "Terraform", "Jenkins", "CI/CD", "GitHub Actions", "GitLab", "Hadoop", "Hive", "HDFS", "Flink", "Beam",
    "Scala", "Golang", "Rust", "C\\+\\+", "C#", "JavaScript", "TypeScript", "React", "Node.js", "Angular",
    "Django", "Spring", "MongoDB", "PostgreSQL", "Postgres", "Oracle", "SQL Server", "Cassandra", "Redis",
    "Elasticsearch", "Tableau", "Looker", "Qlik", "SSIS", "SSRS", "Informatica", "Talend", "Fivetran",
    "Excel", "VBA", "SAS", "SPSS", "MATLAB", "TensorFlow", "PyTorch", "Keras", "XGBoost", "LightGBM",
    "Hugging Face", "Transformers", "LangChain", "LangGraph", "LlamaIndex", "CrewAI", "AutoGen", "OpenAI",
    "GPT-4", "GPT", "Llama", "Mistral", "Pinecone", "Weaviate", "Chroma", "FAISS", "vector database",
    "MLflow", "Kubeflow", "MLOps", "computer vision", "OpenCV", "deep learning", "Microsoft Fabric",
    "Power Automate", "Synapse", "Data Factory", "Unity Catalog", "Delta Live Tables", "Selenium",
    "Playwright", "Postman", "JIRA", "Confluence", "Linux", "Bash", "Shell scripting", "R programming",
    "NoSQL", "GraphQL", "Kotlin", "Swift", "Go",
]
# Terms that are also ordinary English words are matched case-sensitively ("excel at" is fine).
CASE_SENSITIVE = {"Go", "Excel", "Spring", "Glue", "Beam", "Lambda", "Athena", "Chroma", "Swift", "Rust",
                  "Oracle", "Transformers", "Hive", "Flink", "React", "Bash", "Linux", "Llama", "Mistral"}
_TECH_RES = [(t.replace("\\", ""), re.compile(rf"(?<![\w-]){t}(?![\w-])", 0 if t in CASE_SENSITIVE else re.I))
             for t in TECH_TERMS]


@dataclass
class ValidationResult:
    passed: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.passed = False
        self.errors.append(msg)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "errors": self.errors, "warnings": self.warnings}


def _norm(s: str) -> str:
    s = re.sub(r"\*\*|__|`", "", s).replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip().lower()


def _sections(md: str) -> tuple[list[str], dict[str, list[str]], list[str]]:
    head, sections, order, cur = [], {}, [], None
    for line in md.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and not line.startswith("###"):
            cur = m.group(1)
            sections[cur] = []
            order.append(cur)
        elif cur is None:
            head.append(line)
        else:
            sections[cur].append(line)
    return head, sections, order


def _entries(lines: list[str]) -> list[tuple[str, str, list[str]]]:
    out: list[list] = []
    for line in lines:
        if line.startswith("### "):
            out.append([line[4:].strip(), "", []])
        elif out and line.strip().startswith("**") and not out[-1][1]:
            out[-1][1] = line.strip()
        elif out and re.match(r"^\s*-\s*\S", line):
            out[-1][2].append(re.sub(r"^\s*-\s*", "", line).strip())
    return [tuple(e) for e in out]


def validate_resume(md: str, base: BaseResume, job_meta: dict | None = None,
                    gap_terms: list[str] | None = None) -> ValidationResult:
    r = ValidationResult()
    text = (md or "").strip()

    # --- not empty / truncated / malformed
    if len(text) < MIN_CHARS:
        r.fail(f"resume too short ({len(text)} chars); empty or truncated")
        return r
    if len(text) > MAX_CHARS:
        r.fail(f"resume too long ({len(text)} chars)")
    if text.count("**") % 2:
        r.fail("unbalanced ** markup; output looks malformed or truncated")
    if m := PLACEHOLDER_RE.search(text):
        r.fail(f"unresolved placeholder: {m.group(0)!r}")

    head, sections, order = _sections(text)
    head = [ln for ln in head if ln.strip()]

    # --- identity from base resume
    if not head or head[0].strip() != f"# {base.name}":
        r.fail(f"first line must be '# {base.name}'")
    if len(head) < 2 or head[1].strip() != base.contact:
        r.fail("contact line differs from base resume")

    # --- sections
    for name in REQUIRED_SECTIONS:
        if name not in sections:
            r.fail(f"missing section: {name}")
        elif not any(ln.strip() for ln in sections[name]):
            r.fail(f"empty section: {name}")
    for name in order:
        if name not in REQUIRED_SECTIONS:
            r.fail(f"unexpected section: {name}")
    if not r.passed and any(e.startswith("missing section") for e in r.errors):
        return r

    # --- summary
    summary = " ".join(ln.strip() for ln in sections["Professional Summary"] if ln.strip())
    if len(summary) < 80:
        r.fail("professional summary too short")

    # --- skills: groups and items must come from the base resume
    base_groups = {g.name.lower(): {i.lower() for i in g.items} for g in base.skills}
    for line in sections["Technical Skills"]:
        if not line.strip():
            continue
        m = re.match(r"^\s*-\s*\*\*(.+?):\*\*\s*(.+)$", line)
        if not m:
            r.fail(f"malformed skills line: {line.strip()!r}")
            continue
        group = m.group(1).strip().lower()
        if group not in base_groups:
            r.fail(f"skill group not in base resume: {m.group(1)!r}")
            continue
        for item in (i.strip() for i in m.group(2).split(",")):
            if item and item.lower() not in base_groups[group]:
                r.fail(f"skill not in base resume group {m.group(1)!r}: {item!r}")

    # --- experience: same employers/titles/dates, nothing dropped or invented
    base_exp = {e.heading: e for e in base.experience}
    got_exp = _entries(sections["Experience"])
    if {h for h, _, _ in got_exp} != set(base_exp):
        r.fail(f"experience entries must match base resume exactly: {sorted(base_exp)}")
    for heading, meta, bullets in got_exp:
        if heading in base_exp:
            if meta != base_exp[heading].meta:
                r.fail(f"dates changed for {heading!r}")
            if len(bullets) > len(base_exp[heading].bullets):
                r.fail(f"{heading!r} has more bullets than the base resume")

    # --- projects: subset of base, technologies verbatim
    base_proj = {p.heading: p for p in base.projects}
    got_proj = _entries(sections["Selected Projects"])
    if len(got_proj) < min(MIN_PROJECTS, len(base_proj)):
        r.fail(f"at least {min(MIN_PROJECTS, len(base_proj))} projects required")
    for heading, meta, bullets in got_proj:
        if heading not in base_proj:
            r.fail(f"project not in base resume: {heading!r}")
            continue
        if meta != base_proj[heading].meta:
            r.fail(f"technologies line changed for project {heading!r}")
        if not bullets:
            r.fail(f"project {heading!r} has no bullets")
        if len(bullets) > len(base_proj[heading].bullets):
            r.fail(f"project {heading!r} has more bullets than the base resume")

    # --- education and certifications verbatim
    got_edu = {ln.strip() for ln in sections["Education"] if ln.strip()}
    for line in (ln.strip() for ln in base.education_md.splitlines()):
        if line and line not in got_edu:
            r.fail(f"education line changed or missing: {line!r}")
    got_certs = {re.sub(r"^\s*-\s*", "", ln).strip() for ln in sections["Certifications & Achievement"] if ln.strip()}
    if got_certs != set(base.certifications):
        added = got_certs - set(base.certifications)
        r.fail("certifications must match base resume exactly" + (f"; added: {sorted(added)}" if added else ""))

    # --- factual consistency against the whole base resume
    base_norm = _norm(base.raw)
    base_numbers = set(NUMBER_RE.findall(base.raw))
    for num in sorted(set(NUMBER_RE.findall(text)) - base_numbers):
        r.fail(f"number not in base resume: {num!r}")
    for m in EXPERIENCE_CLAIM_RE.finditer(text):
        if _norm(m.group(0)) not in base_norm:
            r.fail(f"experience-length claim not in base resume: {m.group(0)!r}")
    for term, rx in _TECH_RES:
        if rx.search(text) and not rx.search(base.raw):
            r.fail(f"unsupported technical term (not in base resume): {term!r}")
    for gap in gap_terms or []:
        g = _norm(gap)
        if 2 <= len(g) <= 40 and re.search(rf"(?<!\w){re.escape(g)}(?!\w)", _norm(text)) and g not in base_norm:
            r.fail(f"job requirement the candidate lacks appears in resume: {gap!r}")
    for word in ("expert", "seasoned", "extensive experience", "senior", "lead engineer"):
        if re.search(rf"\b{word}\b", text, re.I) and not re.search(rf"\b{word}\b", base.raw, re.I):
            r.fail(f"overstated seniority wording: {word!r}")

    # --- job metadata traceability
    if job_meta is not None:
        for key in ("job_id", "ref_id", "job_url"):
            if key not in job_meta:
                r.fail(f"job metadata missing {key}")
        if not job_meta.get("job_id"):
            r.fail("job metadata job_id is empty")
        if not job_meta.get("job_url"):
            r.fail("job metadata job_url is empty")
    return r


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("resume", type=Path)
    parser.add_argument("--job-id", help="job ID to look up in jobs.xlsx (default: the resume's folder name)")
    args = parser.parse_args(argv)
    from excel_store import JobStore, split_list
    from workspace import get_workspace
    ws = get_workspace()
    meta, gaps = None, None
    job_id = args.job_id or args.resume.resolve().parent.name
    row = JobStore(ws.jobs_xlsx).get(job_id) if ws.jobs_xlsx.is_file() else None
    if row:
        meta = {"job_id": job_id, "ref_id": row.get("Source Reference ID"), "job_url": row.get("Job URL")}
        gaps = split_list(row.get("Missing Skills"))
    result = validate_resume(args.resume.read_text(encoding="utf-8"), load_base_resume(ws.resume), meta, gaps)
    print(json.dumps(result.to_dict(), indent=1))
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
