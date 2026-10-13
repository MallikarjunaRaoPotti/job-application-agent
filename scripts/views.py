"""Per-source view sheets in jobs.xlsx: LinkedIn, Indeed, Naukri.

Each view lists only openings with Match Score >= 8 that were POSTED in the last 24 hours. Closed
or expired top matches are left out. Views are rebuilt from the Jobs sheet on every save, and the
Jobs sheet (the agent's full reference data) is never modified.

Posting times come from the run snapshots in <workspace>/logs (jobs.xlsx has no posted-date column):
an exact timestamp is checked against now - 24 h; a date-only value counts if it is today or yesterday
(India time). Without any known posting date, Date Discovered is used - searches only return jobs
from the last 24 h, so the two are within a day of each other.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

VIEW_SOURCES = ("LinkedIn", "Indeed", "Naukri")
MIN_SCORE = 8.0
HIDE_STATUSES = {"closed", "expired"}
IST = timezone(timedelta(hours=5, minutes=30))

LINK, INK, MUTED, HEAD_BG, TOP_BG = "2A78D6", "0B0B0B", "8A8984", "E8EEF7", "EAF4EA"
THIN = Side(style="thin", color="D9DDE3")

COLUMNS = [("Posted", 18), ("Match Score", 11), ("Status", 26), ("Job Title", 46), ("Company", 30),
           ("Location", 26), ("Work Mode", 11), ("Job Type", 16), ("Salary", 18), ("Matched Skills", 40),
           ("Missing Skills", 40), ("Applied?", 13), ("Job URL", 48), ("Job ID", 18), ("_built", 4)]
STATUS_LABEL = {"verified_top_match": "Top match (> 9.0, open)", "top_match_unverified": "Top match (open unconfirmed)",
                "qualified": "Qualified (8 - 9.0)"}

_cache: dict[str, tuple[float, dict]] = {}


def source_of(url: str | None) -> str:
    u = (url or "").lower()
    if "indeed." in u:
        return "Indeed"
    if "naukri.com" in u:
        return "Naukri"
    return "LinkedIn"


def _read_snapshot(path: Path) -> dict[str, str]:
    """{job_id: posted timestamp or date} from one run snapshot (cached by mtime)."""
    key, mtime = str(path), path.stat().st_mtime
    if key in _cache and _cache[key][0] == mtime:
        return _cache[key][1]
    from scrape_jobs import make_job_id
    out: dict[str, str] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    for hit in data.get("search_hits") or []:                       # LinkedIn search results
        if isinstance(hit, dict) and hit.get("jobId") and hit.get("postedDate"):
            out[make_job_id(str(hit["jobId"]), None)] = str(hit["postedDate"])
    for item in data.get("raw_items") or []:                        # LinkedIn details
        if isinstance(item, dict) and item.get("id") and item.get("datePosted"):
            out.setdefault(make_job_id(str(item["id"]), None), str(item["datePosted"]))
    for job in data.get("jobs") or []:                              # Indeed / Naukri
        if isinstance(job, dict) and job.get("job_id") and (job.get("posted_at") or job.get("posted_date")):
            out[job["job_id"]] = str(job.get("posted_at") or job.get("posted_date"))
    _cache[key] = (mtime, out)
    return out


def posted_dates(logs_dir: Path) -> dict[str, str]:
    dates: dict[str, str] = {}
    if logs_dir.is_dir():
        for snap in sorted(logs_dir.glob("*_discovery.json")) + sorted(logs_dir.glob("*_source_*.json")):
            dates.update(_read_snapshot(snap))
    return dates


def posted_within_24h(value: str | None, now: datetime) -> bool:
    if not value:
        return False
    v = str(value).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        today = now.astimezone(IST).date()
        try:
            d = datetime.strptime(v, "%Y-%m-%d").date()
        except ValueError:
            return False
        return today - timedelta(days=1) <= d <= today
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)   # Naukri text form
        except ValueError:
            return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return now - timedelta(hours=24) <= dt <= now + timedelta(hours=1)


def view_rows(rows: list[dict], source: str, posted: dict[str, str], now: datetime) -> list[dict]:
    out = []
    for r in rows:
        score = r.get("Match Score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or score < MIN_SCORE:
            continue
        if r.get("Processing Status") in HIDE_STATUSES or source_of(r.get("Job URL")) != source:
            continue
        when = posted.get(str(r.get("Job ID"))) or r.get("Date Discovered")
        if posted_within_24h(when, now):
            out.append({**r, "_posted": when})
    return sorted(out, key=lambda r: (-r["Match Score"], str(r.get("Company") or "")))


def _fmt_posted(value: str) -> str:
    v = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
        return v
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        return dt.astimezone(IST).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return v[:16]


def build_views(wb, rows: list[dict], logs_dir: Path | None, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    posted = posted_dates(logs_dir) if logs_dir else {}
    counts = {}
    for i, source in enumerate(VIEW_SOURCES):
        if source in wb.sheetnames:
            del wb[source]
        ws = wb.create_sheet(source, 1 + i)          # right after the Dashboard
        ws.sheet_view.showGridLines = False
        items = view_rows(rows, source, posted, now)
        counts[source] = len(items)
        ws["A1"] = f"{source}: openings scoring 8+ posted in the last 24 hours ({len(items)})"
        ws["A1"].font = Font(bold=True, size=14, color=INK)
        ws["A2"] = (f"Built {now.astimezone(IST).strftime('%d %b %Y, %H:%M')} IST from the Jobs sheet (full data kept "
                    f"there). Rebuilt on every save; pick Applied / Not Applied / Not Working in 'Applied?' and it is "
                    f"copied to the Jobs sheet on the next run.")
        ws["A2"].font = Font(size=9, color=MUTED)
        for c, (name, width) in enumerate(COLUMNS, 1):
            cell = ws.cell(row=4, column=c, value=name)
            cell.font = Font(bold=True, color=INK)
            cell.fill = PatternFill("solid", fgColor=HEAD_BG)
            cell.border = Border(bottom=THIN)
            ws.column_dimensions[cell.column_letter].width = width
        for r_i, r in enumerate(items, 5):
            status = r.get("Processing Status")
            vals = [_fmt_posted(r["_posted"]), r.get("Match Score"), STATUS_LABEL.get(status, status),
                    r.get("Job Title"), r.get("Company"), r.get("Location"), r.get("Work Mode"), r.get("Job Type"),
                    r.get("Salary"), r.get("Matched Skills"), r.get("Missing Skills"), r.get("Application Status"),
                    r.get("Job URL"), r.get("Job ID"), r.get("Application Status")]
            for c, v in enumerate(vals, 1):
                cell = ws.cell(row=r_i, column=c, value=v)
                cell.font = Font(color=INK)
                cell.border = Border(bottom=THIN)
                cell.alignment = Alignment(vertical="top")
                if status == "verified_top_match":
                    cell.fill = PatternFill("solid", fgColor=TOP_BG)
            ws.cell(row=r_i, column=2).number_format = "0.00"
            if r.get("Job URL"):
                link = ws.cell(row=r_i, column=13)
                link.hyperlink = r["Job URL"]
                link.font = Font(color=LINK, underline="single")
        if not items:
            ws.cell(row=5, column=1, value="No openings scoring 8+ posted in the last 24 hours yet.").font = \
                Font(italic=True, color=MUTED)
        ws.freeze_panes = "A5"
        ws.auto_filter.ref = f"A4:N{max(5, 4 + len(items))}"
        ws.column_dimensions["O"].hidden = True          # value at build time, for syncing edits back
        if items:
            from excel_store import add_choice_dropdown
            add_choice_dropdown(ws, f"L5:L{4 + len(items)}")
    return counts
