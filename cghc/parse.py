"""
Read a Chhattisgarh High Court cause-list PDF (highcourt.cg.gov.in/clists).

One PDF holds the whole day (or week) for every court. Each court's block
starts with a heading:

    MONDAY THE 5TH OCTOBER 2026
    HON'BLE THE CHIEF JUSTICE                       <- the bench
    HON'BLE SHRI JUSTICE RAVINDRA KUMAR AGRAWAL
    THE CHIEF JUSTICE'S COURT  |  COURT NO. "5"  |  ADDITIONAL REGISTRAR(J) / COURT NO. "99"
    (AT 02:15 P.M.)
    DAILY CAUSE LIST FOR MONDAY 05/10/2026  |  CAUSE LIST FOR THE WEEK COMMENCING FROM … TO …
    LIST – 1 [ SUPPL. LIST ]
    SNo  Case No  Party Detail  Pet Advocate  Res Advocate      <- column positions

followed by section headings ("FRESH MATTERS", "MOTION HEARING MATTERS") and
items:

    1.  CRMP/2398/2026   AMIR SINGH … AND 2 OTHERS     PUSHP KUMAR GUPTA   A.G.
        (Live Stream - No) VS. STATE OF C.G. AND 1 OTHER
                         TIED UP : HON'BLE SHRI JUSTICE …   <- another judge, not the bench
                         * [ ON ADMISSION ] [ WITH WPCR 398/2026 ] []   <- remark

A centred "WITH" line marks the next item as connected. "List Of Defective
Cases" (filings returned for defects, by filing number) is a different thing
and ends the reading.
"""

from __future__ import annotations

import io
import re
from typing import Dict, List, Optional

import pdfplumber

from common.normalize import clean
from cghc.normalize import parse_case_token, remark_refs

PARSER_VERSION = "cghc-1"

DEFAULT_COLS = {"case": 58.0, "party": 160.0, "pet": 342.0, "res": 450.0}
_WEEKDAY_RE = re.compile(r"^(MONDAY|TUESDAY|WEDNESDAY|THURSDAY|FRIDAY|SATURDAY|SUNDAY) THE ")
_SNO_RE = re.compile(r"^(\d{1,4})(?:\.(\d{1,3}))?\.?$")
_LIST_RE = re.compile(r"^LIST\s*[–-]\s*(\d+\s*[A-Z]?|\d+\s*\(\s*[A-Z]\s*\))", re.I)
_TIME_RE = re.compile(r"^\(AT ([0-9:.]+ ?[AP]\.?M\.?)\)")
_COURT_NO_RE = re.compile(r'^COURT NO\.?\s*"?(\d+)"?')
_DATE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})")


def _lines(page) -> List[List[dict]]:
    words = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
    rows: Dict[int, List[dict]] = {}
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        top = round(w["top"])
        key = next((k for k in rows if abs(k - top) <= 2), top)
        rows.setdefault(key, []).append(w)
    return [sorted(rows[k], key=lambda w: w["x0"]) for k in sorted(rows)]


def _text(line: List[dict]) -> str:
    return clean(" ".join(w["text"] for w in line))


def _judge(text: str) -> str:
    """"HON'BLE SHRI JUSTICE RAVINDRA KUMAR AGRAWAL" -> "Justice Ravindra Kumar Agrawal"."""
    t = re.sub(r"^HON'?BLE\s+", "", text.strip(), flags=re.I)
    if re.match(r"THE CHIEF JUSTICE", t, re.I):
        return "The Chief Justice"
    t = re.sub(r"^(SHRI|SMT\.?|KUM\.?|MS\.?|MR\.?|DR\.?)\s+", "", t, flags=re.I)
    return " ".join(p.capitalize() if not re.match(r"^[A-Z]\.$", p) else p for p in t.split())


def _split_cols(line: List[dict], cols: dict) -> Dict[str, str]:
    out = {"sno": [], "case": [], "party": [], "pet": [], "res": []}
    for w in line:
        x = w["x0"]
        col = ("sno" if x < cols["case"] else "case" if x < cols["party"] else
               "party" if x < cols["pet"] else "pet" if x < cols["res"] else "res")
        out[col].append(w["text"])
    return {k: " ".join(v) for k, v in out.items()}


def _join_names(lines: List[str]) -> str:
    """Advocate names, one per line; a line continuing an open bracket is joined with a space."""
    names: List[str] = []
    for ln in (clean(x) for x in lines):
        if not ln:
            continue
        if names and names[-1].count("(") > names[-1].count(")"):
            names[-1] += " " + ln
        else:
            names.append(ln)
    return ", ".join(names)


def list_label(kind: str, supplementary: bool, week: str = "") -> str:
    if kind == "advance":
        return "Weekly list (tentative)" + (f" · {week}" if week else "")
    return "Supplementary list" if supplementary else "Daily list"


def parse_pdf(data: bytes, kind: str = "daily", supplementary: bool = False, registrar: bool = False) -> dict:
    stats = {"pages": 0, "entries": 0, "blocks": 0, "no_case_number": 0, "unknown_types": {}, "stopped_at_defects": False}
    entries: List[dict] = []
    benches: Dict[str, dict] = {}
    dates: Dict[str, int] = {}
    week = ""
    block = {"judges": [], "court_no": None, "time": None, "list_no": None, "header": False, "cols": dict(DEFAULT_COLS)}
    section: Optional[str] = None
    item: Optional[dict] = None
    connect_next = False

    def finish():
        nonlocal item
        if not item:
            return
        ref, unknown = parse_case_token(item["case_token"])
        if unknown:
            stats["unknown_types"][unknown] = stats["unknown_types"].get(unknown, 0) + 1
        if not ref:
            stats["no_case_number"] += 1
            item = None
            return
        party = clean(" ".join(item["party"]))
        party = re.sub(r"\(\s*Live Stream\s*-\s*(Yes|No)\s*\)", "", party, flags=re.I)
        parts = re.split(r"(?:^|\s)VS\.?(?:\s|$)", party, maxsplit=1, flags=re.I)
        pet, res = (parts[0], parts[1]) if len(parts) == 2 else (party, "")
        remark = clean(" ".join(item["remark"]))
        keys = [ref["key"]] + [k for k in remark_refs(remark) if k != ref["key"]]
        court_no = item["court_no"]
        entries.append({
            "court_no": court_no,
            "item_no": item["sno"],
            "item_main": int(item["sno"].split(".")[0]),
            "item_sub": int(item["sno"].split(".")[1]) if "." in item["sno"] else 0,
            "connected": item["connected"],
            "case_no_raw": item["case_token"],
            "keys": keys,
            "primary_key": ref["key"],
            "petitioner": clean(pet),
            "respondent": clean(res),
            "petitioner_aor": _join_names(item["pet"]),
            "respondent_aor": _join_names(item["res"]),
            "aor_codes": [],
            "section": item["section"],
            "judges": item["judges"],
            "time": item["time"],
            "listed": True,
            "note": remark[:300] or None,
        })
        if court_no and court_no not in benches:
            benches[court_no] = {"court_no": court_no, "judges": item["judges"], "time": item["time"]}
        item = None

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            stats["pages"] += 1
            stop = False
            for line in _lines(page):
                text = _text(line)
                up = text.upper()
                first_x = line[0]["x0"]
                if "LIST OF DEFECTIVE CASES" in up:
                    stats["stopped_at_defects"] = True
                    stop = True
                    break
                if up in ("PRINT",) or up.startswith("BY ORDER OF") or up in ("SD/-",) or up.startswith("REGISTRAR (JUDICIAL)"):
                    continue

                # A new court block.
                if _WEEKDAY_RE.match(up) and first_x > 120:
                    finish()
                    stats["blocks"] += 1
                    block = {"judges": [], "court_no": None, "time": None, "list_no": None, "header": True,
                             "cols": dict(DEFAULT_COLS)}
                    section = None
                    connect_next = False
                    continue
                if block["header"]:
                    if up.startswith("SNO") and "PARTY" in up:
                        xs = {w["text"].upper(): w["x0"] for w in line}
                        block["cols"] = {"case": xs.get("CASE", 61) - 3, "party": xs.get("PARTY", 163) - 3,
                                         "pet": xs.get("PET", 345) - 3, "res": xs.get("RES", 453) - 3}
                        block["header"] = False
                        continue
                    if up.startswith("HON'BLE") or up.startswith("HONBLE"):
                        block["judges"].append(_judge(text))
                    elif "CHIEF JUSTICE'S COURT" in up or "CHIEF JUSTICES COURT" in up:
                        block["court_no"] = "CGCJ"
                    elif _COURT_NO_RE.match(up):
                        n = _COURT_NO_RE.match(up).group(1)
                        block["court_no"] = "CGAR" if n == "99" or registrar else "CG" + n
                    elif "ADDITIONAL REGISTRAR" in up:
                        block["court_no"] = "CGAR"
                        block["judges"] = ["Additional Registrar (Judicial)"]
                    elif _TIME_RE.match(up):
                        block["time"] = _TIME_RE.match(up).group(1)
                    elif _LIST_RE.match(text):
                        block["list_no"] = re.sub(r"\s+", "", _LIST_RE.match(text).group(1))
                    elif "CAUSE LIST FOR" in up:
                        for d in _DATE_RE.findall(up)[:1]:
                            iso = f"{d[2]}-{d[1]}-{d[0]}"
                            dates[iso] = dates.get(iso, 0) + 1
                        if "WEEK" in up:
                            ds = _DATE_RE.findall(up)
                            if len(ds) >= 2:
                                week = f"{ds[0][0]}/{ds[0][1]} – {ds[1][0]}/{ds[1][1]}"
                    continue

                cells = _split_cols(line, block["cols"])
                sno = _SNO_RE.match(cells["sno"].strip()) if cells["sno"] else None
                case_tok = cells["case"].split(" ")[0] if cells["case"] else ""
                if sno and "/" in case_tok:
                    finish()
                    no = sno.group(1) + (f".{sno.group(2)}" if sno.group(2) else "")
                    sect = " · ".join(x for x in [f"List {block['list_no']}" if block["list_no"] else "", section or ""] if x)
                    item = {"sno": no, "case_token": case_tok, "party": [cells["party"]], "pet": [cells["pet"]],
                            "res": [cells["res"]], "remark": [], "mode": "party", "section": sect or None,
                            "court_no": block["court_no"], "judges": list(block["judges"]), "time": block["time"],
                            "connected": connect_next}
                    connect_next = False
                    continue
                # Centred lines between items: section headings, "WITH".
                if not cells["sno"] and not cells["case"] and first_x > 175 and line[-1]["x0"] < block["cols"]["res"] + 60 \
                        and not up.startswith("*") and (item is None or item["mode"] != "party" or first_x > 200) \
                        and not cells["pet"] and not cells["res"]:
                    if up == "WITH":
                        connect_next = True
                    elif re.match(r"^[A-Z][A-Z /&().,'-]+$", up) and len(up) < 70:
                        finish()
                        section = text.title()
                    continue
                if item is None:
                    continue
                party = cells["party"].strip()
                pu = party.upper()
                in_party_col = block["cols"]["party"] <= first_x < block["cols"]["pet"]
                if pu.startswith("*"):
                    # The remark runs the full width of the page.
                    item["mode"] = "remark"
                    item["remark"].append(clean(" ".join([party.lstrip("* ").strip(), cells["pet"], cells["res"]])))
                    continue
                if item["mode"] == "remark" and in_party_col:
                    item["remark"].append(clean(" ".join([party, cells["pet"], cells["res"]])))
                    continue
                if pu.startswith("TIED UP") or pu.startswith("EXCEPTION"):
                    item["mode"] = "tied"
                elif party and item["mode"] == "party":
                    item["party"].append(party)
                if cells["pet"]:
                    item["pet"].append(cells["pet"])
                if cells["res"]:
                    item["res"].append(cells["res"])
            if stop:
                break
        finish()

    stats["entries"] = len(entries)
    return {
        "list_date": max(dates, key=dates.get) if dates else None,
        "kind": kind,
        "list_label": list_label(kind, supplementary, week) if not registrar else "Additional Registrar (Judicial) list",
        "supplementary": supplementary,
        "benches": list(benches.values()),
        "entries": entries,
        "stats": stats,
    }
