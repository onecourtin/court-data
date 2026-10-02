"""
Turn one NCLAT cause-list PDF into the payload the OneCourt app ingests —
the same entry shape as sci/parse.py, so the app matches both courts the
same way.

The table reading itself is nclat/tables.py; this module adds case keys,
splits the parties, names the judges and counts what it could not read.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from typing import List, Optional

import pdfplumber
import io

from common.normalize import clean
from nclat.normalize import _REF_RE, parse_case_numbers
from nclat.tables import parse_tables

_HEAD_DATE_RE = re.compile(r"CAUSE\s+LIST\s+(?:DATED|FOR)\s*:?\s*(\d{2})[./-](\d{2})[./-](\d{4})", re.I)
_VS_RE = re.compile(r"\s+(?:Vs\.?|V/s\.?|Versus|v\.)\s+", re.I)
_VS_TAIL_RE = re.compile(r"\s+(?:Vs\.?|Versus)\s*$", re.I)
_JUDGE_RE = re.compile(
    r"Hon[’'`]?\s*ble\s+(?:Mr\.?|Ms\.?|Mrs\.?|Dr\.?|Smt\.?)?\s*(?:Justice\s+)?"
    r"(?P<name>[A-Z][\w.\s]*?)\s*,\s*(?:the\s+)?(?P<role>[^,]*?(?:Chairperson|Member\s*\([^)]*\)|Member))",
    re.I,
)
_VC_RE = re.compile(r"https?://\S*webex\S*", re.I)
_LEADING_SNO_RE = re.compile(r"^(\d+)\.?")
_NOTE_RE = re.compile(r"\s+Note\s*:.*$", re.I | re.S)
_WITH_TAIL_RE = re.compile(r"\s+(?:WITH|With)\s*$")
_PART_HEARD_RE = re.compile(r"^\s*Part[\s-]*Heard\s*", re.I)


def _judges(bench: str) -> List[str]:
    out = []
    for m in _JUDGE_RE.finditer(bench or ""):
        name = clean(m.group("name"))
        role = clean(m.group("role"))
        is_justice = "justice" in m.group(0).lower()
        person = ("Justice " if is_justice else "") + name
        out.append(f"{person}, {role}")
    return out


def _parties(raw: str, case_raw: str) -> tuple:
    """'A & Anr. Vs. B & Ors.' -> ('A & Anr.', 'B & Ors.'). Drops a case number echoed at the start."""
    text = clean(raw)
    if case_raw and text.startswith(case_raw):
        text = text[len(case_raw):].strip()
    parts = _VS_RE.split(text, maxsplit=1)
    if len(parts) == 2:
        return clean(parts[0]), clean(_WITH_TAIL_RE.sub("", parts[1]))
    return clean(_VS_TAIL_RE.sub("", _WITH_TAIL_RE.sub("", text))), ""


def _split_case_from_parties(parties: str) -> tuple:
    """
    Some rows put the case number at the start of the parties cell:
    'Comp. App. (AT) (Ins) No. 210 of 2022 & I.A. No. 3240 of 2022 Part Heard A Vs. B'.
    Returns (case_text, parties_text); ('', parties) when it doesn't start with one.
    """
    end = 0
    for m in _REF_RE.finditer(parties):
        gap = parties[end:m.start()] + m.group("before")
        if len(gap) > 60 or (end == 0 and m.start() > 0):
            break
        end = m.end()
    if not end:
        return "", parties
    tail = re.match(r"(?:\s*,\s*[\d,\s]+(?:of|/)\s*(?:19|20)\d{2})*", parties[end:])
    end += tail.end()
    return clean(parties[:end]), _PART_HEARD_RE.sub("", parties[end:]).strip()


_PARTY_END_RE = re.compile(r"(?:&\s*(?:Anr|Ors)\.?|Ltd\.?|Limited|LLP|India)(?=\s)", re.I)


def _parties_from_counsel(text: str) -> tuple:
    """'A Vs. B & Anr. With Kunal Mimani' -> ('A Vs. B & Anr.', 'Kunal Mimani'), best effort."""
    vs = _VS_RE.search(text)
    m = re.search(r"\s+With\s+", text[vs.end():]) if vs else None
    if m:
        cut = vs.end() + m.start()
        return text[:cut], clean(text[vs.end() + m.end():])
    ends = list(_PARTY_END_RE.finditer(text, vs.end())) if vs else []
    if ends:
        return text[:ends[-1].end()], clean(text[ends[-1].end():])
    return text, ""


def _header(data: bytes) -> dict:
    out = {"list_date": None, "vc_link": None}
    with pdfplumber.open(io.BytesIO(data)) as pdf:
        text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
        out["pages"] = len(pdf.pages)
    m = _HEAD_DATE_RE.search(text)
    if m:
        out["list_date"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    v = _VC_RE.search(text)
    if v:
        out["vc_link"] = v.group(0).rstrip(").,")
    return out


def list_label(court_label: str, supplementary: bool) -> str:
    """The court itself is shown from court_no ("NCLAT Court II"), so only the list kind here."""
    return "Supplementary cause list" if supplementary else "Daily cause list"


def parse_pdf(data: bytes, court_no: str, court_label: str, bench: str = "delhi",
              supplementary: bool = False) -> dict:
    head = _header(data)
    tables = parse_tables(data)
    chennai = bench == "chennai"
    stats = {"pages": head.get("pages", 0), "items": len(tables["items"]), "entries": 0,
             "no_case_number": 0, "unknown_types": {}}
    benches: "OrderedDict[str, dict]" = OrderedDict()
    entries: List[dict] = []

    for it in tables["items"]:
        sm = _LEADING_SNO_RE.match(clean(it.get("sno") or it.get("s.no") or ""))
        if not sm:
            continue
        sno = sm.group(1)
        case_raw = clean(it.get("case_no", ""))
        parties_raw = it.get("parties", "")
        if not case_raw:
            case_raw, parties_raw = _split_case_from_parties(clean(parties_raw))
        refs, unknown = parse_case_numbers(case_raw, chennai_list=chennai)
        for u in unknown:
            stats["unknown_types"][u] = stats["unknown_types"].get(u, 0) + 1
        if not refs:
            stats["no_case_number"] += 1
        counsel_app = clean(it.get("counsel_appellant", ""))
        if not clean(parties_raw) and _VS_RE.search(counsel_app):
            # The parties slid into the counsel column ("A Vs. B & Anr. With Counsel").
            parties_raw, counsel_app = _parties_from_counsel(counsel_app)
        pet, res = _parties(parties_raw, case_raw)
        bench_text = _NOTE_RE.sub("", clean(it.get("_bench", "")))
        judges = _judges(bench_text)
        b = benches.setdefault(bench_text or court_no, {
            "court_no": court_no, "label": court_label, "judges": judges,
            "time": it.get("_time"), "vc_link": head["vc_link"],
        })
        if not b["time"] and it.get("_time"):
            b["time"] = it["_time"]
        entries.append({
            "court_no": court_no,
            "item_no": sno,
            "item_main": int(sno),
            "item_sub": 0,
            "connected": False,
            "case_no_raw": case_raw,
            "keys": [r["key"] for r in refs],
            "primary_key": refs[0]["key"] if refs else None,
            "petitioner": pet,
            "respondent": res,
            # NCLAT lists name counsel, not AoR codes; same fields as SCI.
            "petitioner_aor": counsel_app,
            "respondent_aor": clean(it.get("counsel_respondent", "")),
            "aor_codes": [],
            "section": clean(it.get("_section", "")) or None,
            "judges": judges,
            "time": it.get("_time"),
            "listed": True,
            "note": None,
        })

    stats["entries"] = len(entries)
    stats["unknown_types"] = dict(sorted(stats["unknown_types"].items(), key=lambda kv: -kv[1]))
    return {
        "list_date": head["list_date"],
        "kind": "daily",
        "list_label": list_label(court_label, supplementary),
        "supplementary": supplementary,
        "benches": list(benches.values()),
        "entries": entries,
        "stats": stats,
    }
