"""Parse the base resume (<workspace>/resume.md) into an ID-addressed structure.

Every unit the tailoring model may touch gets a stable ID, so a tailored resume can be traced
back to the base resume line by line:
  skill groups  SK1..      experience  EXP1.. (bullets EXP1.b1..)
  projects      PROJ1..    (bullets PROJ1.b1..)
Header, contact line, dates, employers, education and certifications are kept verbatim and are
never sent for rewriting.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

SECTION_SUMMARY = "Professional Summary"
SECTION_SKILLS = "Technical Skills"
SECTION_EXPERIENCE = "Experience"
SECTION_PROJECTS = "Selected Projects"
SECTION_EDUCATION = "Education"
SECTION_CERTS = "Certifications & Achievement"
REQUIRED_SECTIONS = [SECTION_SUMMARY, SECTION_SKILLS, SECTION_EXPERIENCE, SECTION_PROJECTS,
                     SECTION_EDUCATION, SECTION_CERTS]


class ResumeParseError(ValueError):
    pass


@dataclass
class Bullet:
    id: str
    text: str


@dataclass
class SkillGroup:
    id: str
    name: str
    items: list[str]


@dataclass
class Entry:
    """An experience role or a project."""
    id: str
    heading: str              # "Company — Engineering Intern" / project name
    meta: str                 # "**Jun 2026 – Oct 2026**" / "**Technologies:** ..."
    bullets: list[Bullet] = field(default_factory=list)


@dataclass
class BaseResume:
    name: str
    contact: str
    summary: str
    skills: list[SkillGroup]
    experience: list[Entry]
    projects: list[Entry]
    education_md: str         # verbatim block
    certifications: list[str]
    raw: str

    def bullet(self, bullet_id: str) -> Bullet | None:
        for entry in self.experience + self.projects:
            for b in entry.bullets:
                if b.id == bullet_id:
                    return b
        return None

    def entry(self, entry_id: str) -> Entry | None:
        return next((e for e in self.experience + self.projects if e.id == entry_id), None)

    def skill_group(self, group_id: str) -> SkillGroup | None:
        return next((g for g in self.skills if g.id == group_id), None)

    def for_prompt(self) -> str:
        """ID-annotated view of the rewritable parts, sent to the tailoring model (no contact info)."""
        out = [f"NAME: {self.name}", "", f"SUMMARY (base): {self.summary}", "", "SKILL GROUPS:"]
        out += [f"  {g.id} {g.name}: {', '.join(g.items)}" for g in self.skills]
        out += ["", "EXPERIENCE:"]
        for e in self.experience:
            out.append(f"  {e.id} {e.heading} | {strip_md(e.meta)}")
            out += [f"    {b.id}: {b.text}" for b in e.bullets]
        out += ["", "PROJECTS:"]
        for p in self.projects:
            out.append(f"  {p.id} {p.heading} | {strip_md(p.meta)}")
            out += [f"    {b.id}: {b.text}" for b in p.bullets]
        out += ["", "EDUCATION (fixed, not rewritable):", strip_md(self.education_md),
                "", "CERTIFICATIONS (fixed, not rewritable):"] + [f"  - {c}" for c in self.certifications]
        return "\n".join(out)


def strip_md(text: str) -> str:
    return re.sub(r"\*\*|__|`", "", text).strip()


def _split_sections(text: str) -> tuple[list[str], dict[str, list[str]]]:
    head, sections, current = [], {}, None
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and not line.startswith("###"):
            current = m.group(1)
            sections[current] = []
        elif current is None:
            head.append(line)
        else:
            sections[current].append(line)
    return head, sections


def _bullets(lines: list[str]) -> list[str]:
    return [re.sub(r"^\s*-\s*", "", ln).strip() for ln in lines if re.match(r"^\s*-\s*\S", ln)]


def _entries(lines: list[str], prefix: str) -> list[Entry]:
    entries: list[Entry] = []
    for line in lines:
        m = re.match(r"^###\s+(.+?)\s*$", line)
        if m:
            entries.append(Entry(id=f"{prefix}{len(entries) + 1}", heading=m.group(1), meta=""))
        elif entries and re.match(r"^\s*\*\*.+", line) and not entries[-1].meta:
            entries[-1].meta = line.strip()
        elif entries and re.match(r"^\s*-\s*\S", line):
            e = entries[-1]
            e.bullets.append(Bullet(id=f"{e.id}.b{len(e.bullets) + 1}", text=re.sub(r"^\s*-\s*", "", line).strip()))
    return entries


def parse_resume(text: str) -> BaseResume:
    head, sections = _split_sections(text)
    missing = [s for s in REQUIRED_SECTIONS if s not in sections]
    if missing:
        raise ResumeParseError(f"base resume missing sections: {missing}")
    head = [ln for ln in head if ln.strip()]
    if not head or not head[0].startswith("# "):
        raise ResumeParseError("base resume must start with '# <Name>'")
    name = head[0][2:].strip()
    contact = head[1].strip() if len(head) > 1 else ""

    skills = []
    for item in _bullets(sections[SECTION_SKILLS]):
        m = re.match(r"\*\*(.+?):\*\*\s*(.+)", item)
        if not m:
            raise ResumeParseError(f"skill line not in '- **Group:** a, b' format: {item!r}")
        items = [s.strip() for s in m.group(2).split(",") if s.strip()]
        skills.append(SkillGroup(id=f"SK{len(skills) + 1}", name=m.group(1).strip(), items=items))

    summary = " ".join(ln.strip() for ln in sections[SECTION_SUMMARY] if ln.strip())
    education_md = "\n".join(sections[SECTION_EDUCATION]).strip()
    return BaseResume(
        name=name, contact=contact, summary=summary, skills=skills,
        experience=_entries(sections[SECTION_EXPERIENCE], "EXP"),
        projects=_entries(sections[SECTION_PROJECTS], "PROJ"),
        education_md=education_md, certifications=_bullets(sections[SECTION_CERTS]), raw=text,
    )


def load_base_resume(path: Path | None = None) -> BaseResume:
    if path is None:
        from workspace import get_workspace
        path = get_workspace().resume
    return parse_resume(path.read_text(encoding="utf-8"))
