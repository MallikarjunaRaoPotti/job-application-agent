"""jobs.xlsx: the local job tracker and the cross-run dedup store (keyed by Job ID).

Rules:
- Existing rows are preserved; the workbook is loaded and updated in place, never recreated.
- Columns are located by header name, so reordered or extra user columns are tolerated.
- "Application Status" is the user's column: set to "Not Applied" on insert, never overwritten.
- Saves go to a temp file then atomically replace jobs.xlsx, keeping jobs.xlsx.bak.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font

SHEET = "Jobs"
COLUMNS = [
    "Job ID", "Source Reference ID", "Job Title", "Company", "Location", "Job Type", "Work Mode", "Salary",
    "Job URL", "Job Description", "Required Skills", "Preferred Skills", "Matched Skills", "Missing Skills",
    "Match Score", "Application Status", "Date Discovered", "Resume Path", "Tailoring Report Path",
    "Processing Status", "Last Updated", "Error Details",
]
USER_OWNED = {"Application Status"}
DEFAULT_APPLICATION_STATUS = "Not Applied"
LIST_SEP = "; "
MAX_CELL = 32000  # Excel's hard limit is 32,767 characters per cell
WIDTHS = {"Job ID": 18, "Job Title": 40, "Company": 30, "Job URL": 45, "Job Description": 60,
          "Resume Path": 60, "Tailoring Report Path": 60, "Error Details": 50}


class WorkbookLockedError(RuntimeError):
    """jobs.xlsx can't be written, usually because it's open in Excel."""


def _cell_value(value):
    if isinstance(value, (list, tuple)):
        value = LIST_SEP.join(str(v) for v in value if str(v).strip())
    if isinstance(value, str):
        value = value[:MAX_CELL]
    return value


def split_list(value) -> list[str]:
    if not value:
        return []
    return [v.strip() for v in str(value).split(LIST_SEP.strip()) if v.strip()]


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class JobStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        if self.path.exists():
            try:
                self.wb = load_workbook(self.path)
            except PermissionError as exc:
                raise WorkbookLockedError(f"cannot open {self.path}; close it in Excel and re-run") from exc
            self.ws = self.wb[SHEET] if SHEET in self.wb.sheetnames else self.wb.create_sheet(SHEET)
        else:
            self.wb = Workbook()
            self.ws = self.wb.active
            self.ws.title = SHEET
        self._ensure_headers()
        self._rows = self._index_rows()

    # ---------------------------------------------------------------- structure

    def _ensure_headers(self) -> None:
        ws = self.ws
        existing = [c.value for c in ws[1]] if ws.max_row >= 1 else []
        if not any(existing):
            existing = []
        for name in COLUMNS:
            if name not in existing:
                existing.append(name)
                cell = ws.cell(row=1, column=len(existing), value=name)
                cell.font = Font(bold=True)
                letter = cell.column_letter
                ws.column_dimensions[letter].width = WIDTHS.get(name, 18)
        ws.freeze_panes = "B2"
        self.col = {c.value: c.column for c in ws[1] if c.value}

    def _index_rows(self) -> dict[str, int]:
        rows, jcol = {}, self.col["Job ID"]
        for r in range(2, self.ws.max_row + 1):
            jid = self.ws.cell(row=r, column=jcol).value
            if jid:
                rows[str(jid)] = r
        return rows

    # ---------------------------------------------------------------- read

    def __contains__(self, job_id: str) -> bool:
        return job_id in self._rows

    def __len__(self) -> int:
        return len(self._rows)

    def get(self, job_id: str) -> dict | None:
        r = self._rows.get(job_id)
        if r is None:
            return None
        return {name: self.ws.cell(row=r, column=c).value for name, c in self.col.items()}

    def all(self) -> list[dict]:
        return [self.get(j) for j in self._rows]

    def dedup_index(self) -> dict:
        """Index in the shape scrape_jobs.deduplicate() expects."""
        index = {}
        for row in self.all():
            index[str(row["Job ID"])] = {
                "ref_id": row["Source Reference ID"], "title": row["Job Title"], "company": row["Company"],
                "link": row["Job URL"], "first_seen_at": row["Date Discovered"], "last_seen_at": row["Last Updated"],
                "job_status": row["Processing Status"],
            }
        return index

    # ---------------------------------------------------------------- write

    def upsert(self, job_id: str, values: dict) -> str:
        """Insert or update one row by Job ID. Returns 'inserted' or 'updated'."""
        unknown = set(values) - set(self.col)
        if unknown:
            raise KeyError(f"unknown columns: {sorted(unknown)}")
        r = self._rows.get(job_id)
        action = "updated"
        if r is None:
            r = self.ws.max_row + 1
            self._rows[job_id] = r
            action = "inserted"
            self.ws.cell(row=r, column=self.col["Job ID"], value=job_id)
            self.ws.cell(row=r, column=self.col["Application Status"], value=DEFAULT_APPLICATION_STATUS)
        for name, value in values.items():
            if name == "Job ID" or (name in USER_OWNED and action == "updated"):
                continue
            cell = self.ws.cell(row=r, column=self.col[name])
            cell.value = _cell_value(value)  # ws.cell(value=None) would silently keep the old value
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"  # scraped text must never become a formula
            if name in ("Job Description", "Error Details"):
                cell.alignment = Alignment(wrap_text=False, vertical="top")
        self.ws.cell(row=r, column=self.col["Last Updated"], value=now_iso())
        return action

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ws.auto_filter.ref = self.ws.dimensions  # filter dropdowns on every column
        from dashboard import build_dashboard     # summary sheet, rebuilt from the rows on every save
        build_dashboard(self.wb, self.all())
        self.wb.active = self.wb.sheetnames.index(SHEET)
        tmp = self.path.with_name(self.path.stem + ".tmp.xlsx")
        try:
            self.wb.save(tmp)
            if self.path.exists():
                shutil.copy2(self.path, self.path.with_suffix(".xlsx.bak"))
            os.replace(tmp, self.path)
        except PermissionError as exc:
            tmp.unlink(missing_ok=True)
            raise WorkbookLockedError(f"cannot write {self.path}; close it in Excel and re-run") from exc
