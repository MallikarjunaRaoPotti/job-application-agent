"""Claude Code hook: validate tailored resumes written during an interactive session.

Target: <workspace>/output/resumes/<job_id>/resume.md (never the base resume).
- PreToolUse(Write): validates the content about to be written; exit 2 blocks the write.
- PostToolUse(Edit|MultiEdit): validates the edited file; exit 2 feeds the errors back to Claude
  and marks the job's jobs.xlsx row validation_failed so it can't count as complete.
Job metadata (URL, missing skills) comes from the job's jobs.xlsx row when available.
Shares scripts/validate_resume.py with the pipeline. Other files pass through untouched.
"""

import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "scripts"))


def _job_row(xlsx: Path, job_id: str):
    try:
        from excel_store import JobStore
        return JobStore(xlsx).get(job_id) if xlsx.is_file() else None
    except Exception:
        return None


def main() -> int:
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        event = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    tool_input = event.get("tool_input") or {}
    path = Path(tool_input.get("file_path") or "")
    if path.name != "resume.md" or path.parent.parent.name != "resumes":
        return 0

    from excel_store import split_list
    from resume_parser import load_base_resume
    from validate_resume import validate_resume

    pre = event.get("hook_event_name") == "PreToolUse"
    if pre:
        content = tool_input.get("content") or ""
    elif path.is_file():
        content = path.read_text(encoding="utf-8")
    else:
        return 0

    workspace = path.parents[3]  # <workspace>/output/resumes/<job_id>/resume.md
    job_id = path.parent.name
    xlsx = workspace / "jobs.xlsx"
    row = _job_row(xlsx, job_id)
    meta = {"job_id": job_id, "ref_id": row.get("Source Reference ID"), "job_url": row.get("Job URL")} if row else None
    gaps = split_list(row.get("Missing Skills")) if row else []
    base_path = workspace / "resume.md"
    base = load_base_resume(base_path if base_path.is_file() else None)

    result = validate_resume(content, base, meta, gaps)
    if result.passed:
        return 0
    if row and not pre:
        try:
            from excel_store import JobStore
            store = JobStore(xlsx)
            store.upsert(job_id, {"Processing Status": "validation_failed",
                                  "Error Details": "; ".join(result.errors[:10])})
            store.save()
        except Exception as exc:  # workbook open in Excel etc.; the exit code still blocks
            result.errors.append(f"(could not update jobs.xlsx: {exc})")
    print("resume.md failed validation (the base resume is the only source of truth):\n- "
          + "\n- ".join(result.errors), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
