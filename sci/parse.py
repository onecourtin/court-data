"""
Parser for Supreme Court of India cause-list PDFs: daily (main and
supplementary), chamber, single-judge, registrar and advance lists.

Every list is the same four-column table:

    SNo. | Case No. | Petitioner / Respondent | Advocate ("NAME- AOR CODE")

Column positions come from each page's header row ("SNo. Case No.
Petitioner / Respondent … Advocate"); pages without one reuse the last
positions seen. Words are assigned to a column by their x position, which is
what keeps split case numbers ("SLP(Crl) No." / "16213/2026") and AoR codes
on the following line together.

Page furniture (court headers, judges, notes, section headings) is read for
context and never mixed into item text. "DROP NOTE" tables (items shifted or
deleted) are skipped.
"""

from __future__ import annotations

import io
import re
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

import pdfplumber

from common.normalize import clean, extract_aor_codes, parse_case_numbers

A4_WIDTH = 595.28
# x0 of SNo | Case No. | Petitioner/Respondent | Advocate on an A4 page.
DEFAULT_COLS = (43.0, 69.0, 185.0, 426.0)

RE_DATE_DAILY = re.compile(r"CAUSE LIST FOR DATED\s*:?\s*(\d{2})-(\d{2})-(\d{4})", re.I)
RE_DATE_ADV = re.compile(r"MATTERS TO BE LISTED ON\s*:?\s*(\d{2})-(\d{2})-(\d{4})", re.I)
RE_COURT = re.compile(r"^(?:IN\s+)?COURT\s+NO\.?\s*:?\s*(\d+)\b", re.I)
RE_CJ_COURT = re.compile(r"^CHIEF\s+JUSTICE'?S?\s+COURT", re.I)
RE_REG_COURT = re.compile(r"^REGISTRAR\s+COURT\s+NO\.?\s*:?\s*(\d+)", re.I)
RE_JUDGE = re.compile(r"^HON'?BLE\s+(.*)$", re.I)
RE_REGISTRAR = re.compile(r"^(?:SH|SMT|MS|MR|DR|KUM)\.?\s+(.+?),\s*((?:ADDITIONAL\s+|DEPUTY\s+|ASSISTANT\s+)?REGISTRAR.*)$", re.I)
RE_TIME = re.compile(r"^\(?\s*TIME\s*:\s*([^)]+?)\s*\)?$", re.I)
# "[BAIL MATTERS]", "[SERVICE/COMPLIANCE]-BEFORE REGISTRAR(J)"
RE_SECTION = re.compile(r"^\[\s*([^\]]+?)\s*\]\s*(-?\s*[A-Z][A-Z()/.&\s-]*)?$")
RE_SNO = re.compile(r"^(\d{1,5})(?:\.(\d{0,3}))?$")   # "12", "12.3", and "517." (wrapped)
RE_BENCH_CODE = re.compile(r"^[IVXL]+(?:-[A-Z]{1,2})?$")
RE_STAMP = re.compile(r"\b\d{2}-\d{2}-\d{4}\s+\d{2}:\d{2}(?::\d{2})?\b|\b\d{2}:\d{2}:\d{2}\b")
RE_ONLY_LISTED = re.compile(r"^ONLY\b.*\bLISTED\b", re.I)

VERSUS = {"VERSUS", "VERSUS.", "VS", "VS.", "V/S", "V/S."}

# Headings that set the hearing context. They also turn off drop-note mode,
# since a drop-note table always ends before the next list section starts.
HEARING_HEADINGS = {
    "MISCELLANEOUS HEARING", "REGULAR HEARING", "CHAMBER MATTERS",
    "MISCELLANEOUS MATTERS", "REGULAR MATTERS", "FINAL HEARING",
}

# Whole-line text that is never part of an item.
SKIP_EXACT = {
    "SUPREME COURT OF INDIA", "PETITIONER/RESPONDENT", "ADVOCATE", "NEW DELHI",
    "FOR ADMISSION", "FOR ORDERS", "FOR HEARING", "FOR FINAL HEARING",
    "AFTER NOTICE", "FRESH MATTERS", "FOR FINAL DISPOSAL",
}
SKIP_PREFIXES = (
    "[ IT WILL BE APPRECIATED", "[IT WILL BE APPRECIATED", "ON RECORD DO NOT SEEK",
    "LISTED BEFORE ALL THE COURTS", "ADVANCE LIST", "TENTATIVE LIST",
    "NOTE", "THIS BENCH WILL", "ADDITIONAL REGISTRAR", "DEPUTY REGISTRAR",
    "ASSISTANT REGISTRAR", "NEW DELHI",
)

# Lines inside an item, after the respondent, that start the item's "tail"
# (applications, mention memos, listing notes) rather than continue a name.
TAIL_PREFIXES = (
    "IA NO", "IA ", "I.A.", "{", "FOR ", "ONLY ", "[", "WITH IA", "OFFICE REPORT",
    "TAGGED", "PASSED OVER", "(FOR ", "LISTED ",
)


# ---------------------------------------------------------------------------
# Page → lines → cells
# ---------------------------------------------------------------------------

def _page_lines(page) -> List[dict]:
    words = page.extract_words(x_tolerance=3, y_tolerance=2.5, keep_blank_chars=False)
    words.sort(key=lambda w: (w["top"], w["x0"]))
    lines: List[dict] = []
    for w in words:
        if lines and abs(w["top"] - lines[-1]["top"]) <= 2.5:
            lines[-1]["words"].append(w)
        else:
            lines.append({"top": w["top"], "words": [w]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w["x0"])
        ln["text"] = clean(" ".join(w["text"] for w in ln["words"]))
    return lines


def _find_header(lines: List[dict]) -> Tuple[Optional[int], Optional[Tuple[float, float, float, float]]]:
    """Locate the 'SNo. Case No. Petitioner / Respondent' row and read column x positions."""
    for i, ln in enumerate(lines):
        texts = [w["text"] for w in ln["words"]]
        if "SNo." not in texts or "Case" not in texts:
            continue
        sno_x = next(w["x0"] for w in ln["words"] if w["text"] == "SNo.")
        case_x = next(w["x0"] for w in ln["words"] if w["text"] == "Case")
        pet = [w["x0"] for w in ln["words"] if w["text"].startswith("Petitioner")]
        party_x = pet[0] if pet else None
        adv_x = None
        # The right-hand "Petitioner/Respondent / Advocate" heading sits a few
        # points above and below the main row.
        for other in lines[max(0, i - 3): i + 4]:
            for w in other["words"]:
                if w["text"] in ("Advocate", "Petitioner/Respondent") and w["x0"] > (party_x or case_x) + 100:
                    adv_x = w["x0"] if adv_x is None else min(adv_x, w["x0"])
        if party_x is None or adv_x is None:
            return i, None
        return i, (sno_x, case_x, party_x, adv_x)
    return None, None


def _default_cols(page_width: float) -> Tuple[float, float, float, float]:
    s = page_width / A4_WIDTH
    return tuple(x * s for x in DEFAULT_COLS)  # type: ignore[return-value]


def _cells(words: List[dict], cols: Tuple[float, float, float, float]) -> List[str]:
    _, case_x, party_x, adv_x = cols
    b1, b2, b3 = case_x - 4, party_x - 4, adv_x - 4
    out: List[List[str]] = [[], [], [], []]
    for w in words:
        x = w["x0"]
        col = 0 if x < b1 else 1 if x < b2 else 2 if x < b3 else 3
        out[col].append(w["text"])
    return [clean(" ".join(c)) for c in out]


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

def _sno_wraps(item: Optional[dict], token: str) -> bool:
    """
    The SNo. column is narrow, so long connected-item numbers wrap onto the
    next line (the one that carries the case number):
      "56.1" + "0" -> 56.10        "517." + "1" -> 517.1
    """
    if item is None or not token.isdigit() or item["case"]:
        return False
    if item["pending_sub"]:
        return len(token) <= 3
    return 0 < item["sub"] < 10 and len(token) == 1


def _new_item(sno: str, ctx: dict) -> dict:
    main, dot, sub = sno.partition(".")
    pending = bool(dot) and not sub
    return {
        "sno": sno, "main": int(main), "sub": int(sub) if sub else 0, "pending_sub": pending,
        "court_no": ctx["court_no"], "section": ctx["section"],
        "judges": list(ctx["bench"]), "time": ctx["time"],
        "connected": bool(sub) or pending, "case": [], "section_code": None,
        "pet": [], "res": [], "pet_aor": [], "res_aor": [],
        "stage": "pet", "tail": [], "only": [],
    }


def _feed(item: dict, cells: List[str]) -> None:
    """Add one table row's cells to the open item."""
    _, case_cell, party_cell, adv_cell = cells

    if case_cell:
        t = case_cell
        if re.search(r"\bConnected\b", t, re.I):
            item["connected"] = True
            t = clean(re.sub(r"\bConnected\b", " ", t, flags=re.I))
        if t:
            # The bench/subject code ("II-A") usually has its own line, but
            # sometimes trails the case number on the same line.
            head, _, last = t.rpartition(" ")
            if RE_BENCH_CODE.match(t):
                item["section_code"] = t
                t = ""
            elif head and RE_BENCH_CODE.match(last):
                item["section_code"] = last
                t = head
            if t and item["stage"] == "pet":
                item["case"].append(t)

    if party_cell:
        up = party_cell.upper()
        if up in VERSUS:
            item["stage"] = "res"
        elif RE_ONLY_LISTED.match(party_cell):
            item["only"].append(party_cell)
            if item["stage"] == "res":
                item["stage"] = "tail"
        elif item["stage"] == "pet":
            item["pet"].append(party_cell)
        elif item["stage"] == "res":
            if (up.startswith(TAIL_PREFIXES) or up == "FOR") and item["res"]:
                item["stage"] = "tail"
                item["tail"].append(party_cell)
            else:
                item["res"].append(party_cell)
        else:
            item["tail"].append(party_cell)

    if adv_cell:
        (item["pet_aor"] if item["stage"] == "pet" else item["res_aor"]).append(adv_cell)


def _finish(item: dict, stats: dict) -> dict:
    case_raw = clean(" ".join(item["case"]))
    refs, unknown = parse_case_numbers(case_raw)
    for u in unknown:
        stats["unknown_types"][u] = stats["unknown_types"].get(u, 0) + 1
    if not refs:
        stats["no_case_number"] += 1
    only_keys: List[str] = []
    for note in item["only"]:
        only_keys += [r["key"] for r in parse_case_numbers(note)[0]]
    pet_aor = clean(" ".join(item["pet_aor"]))
    res_aor = clean(" ".join(item["res_aor"]))
    return {
        "court_no": item["court_no"],
        "item_no": item["sno"],
        "item_main": item["main"],
        "item_sub": item["sub"],
        "connected": item["connected"],
        "case_no_raw": case_raw,
        "keys": [r["key"] for r in refs],
        "primary_key": next((r["key"] for r in refs if r["role"] == "primary"), None),
        "petitioner": clean(" ".join(item["pet"])),
        "respondent": clean(" ".join(item["res"])),
        "petitioner_aor": pet_aor,
        "respondent_aor": res_aor,
        "aor_codes": extract_aor_codes(pet_aor + " " + res_aor),
        "section": item["section"],
        "section_code": item["section_code"],
        "judges": item["judges"],
        "time": item["time"],
        "listed": True,
        "note": clean(" ".join(item["only"])) or None,
        "_only_keys": only_keys,
    }


def _apply_only_listed(entries: List[dict]) -> None:
    """
    "Only C.A. No. 8980/2026 is listed under this item" means the other cases
    grouped under that item number are NOT listed today. Mark them so no
    listing alert goes out for them.
    """
    groups: Dict[Tuple[Optional[str], int], List[dict]] = {}
    for e in entries:
        groups.setdefault((e["court_no"], e["item_main"]), []).append(e)
    for group in groups.values():
        only = {k for e in group for k in e["_only_keys"]}
        if not only:
            continue
        for e in group:
            e["listed"] = bool(set(e["keys"]) & only)
    for e in entries:
        e.pop("_only_keys", None)


# ---------------------------------------------------------------------------
# Context lines (court, judges, sections …)
# ---------------------------------------------------------------------------

def _judge_name(rest: str) -> str:
    rest = clean(rest)
    up = rest.upper()
    if "CHIEF JUSTICE" in up:
        name = re.sub(r"^(?:THE\s+)?CHIEF\s+JUSTICE\s*", "", rest, flags=re.I).strip(" ,")
        return ("The Chief Justice " + name.title()).strip() if name else "The Chief Justice"
    name = re.sub(r"^(?:THE\s+)?(?:MR\.|MRS\.|MS\.|DR\.|MISS\.?)?\s*JUSTICE\s*", "", rest, flags=re.I)
    return "Justice " + name.title().strip()


def _court_label(court_no: Optional[str]) -> Optional[str]:
    if not court_no:
        return None
    if court_no.startswith("R"):
        return f"Registrar Court No. {court_no[1:]}"
    if court_no == "1":
        return "Court No. 1 (Chief Justice's Court)"
    return f"Court No. {court_no}"


def _context(text: str, ctx: dict, benches: "OrderedDict[str, dict]", in_header: bool,
             left_of_advocates: bool = True) -> Optional[str]:
    """
    Handle a page-furniture line. `in_header` is True above the table's
    column-heading row, the only place judges, registrars and sitting times
    are printed (a party can be called "HON'BLE GOVERNOR …"). Returns:
      "court"   – a different court starts (close the open item)
      "section" – a new section/heading starts (close the open item)
      "skip"    – furniture; ignore the line
      None      – not furniture; treat as table content
    """
    up = text.upper()

    if "DROP NOTE" in up:
        ctx["drop"] = True
        return "section"

    m = RE_DATE_DAILY.search(up) or RE_DATE_ADV.search(up)
    if m:
        ctx["list_date"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        if RE_DATE_ADV.search(up):
            ctx["kind"] = "advance"
        return "skip"

    court = None
    m = RE_REG_COURT.match(up)
    if m:
        court = "R" + m.group(1)
    elif RE_CJ_COURT.match(up):
        court = "1"
    else:
        m = RE_COURT.match(up)
        if m:
            court = m.group(1)
    if court is not None:
        if in_header:
            # A header block starts: the judges printed under it form the
            # bench for the items that follow (a court can sit as more than
            # one bench in a day, e.g. a special bench before the regular one).
            ctx["bench"], ctx["time"] = [], None
        if court != ctx["court_no"]:
            ctx.update(court_no=court, section=None, drop=False)
            benches.setdefault(court, {"court_no": court, "label": _court_label(court), "judges": [], "time": None})
            return "court"
        return "skip"

    m = RE_JUDGE.match(text) if in_header else None
    name = _judge_name(m.group(1)) if m else None
    if name is None:
        m = RE_REGISTRAR.match(text) if in_header else None
        name = f"{m.group(1).title()}, {m.group(2).title()}" if m else None
    if name is not None:
        if name not in ctx["bench"]:
            ctx["bench"].append(name)
        if ctx["court_no"] in benches and name not in benches[ctx["court_no"]]["judges"]:
            benches[ctx["court_no"]]["judges"].append(name)
        return "skip"

    m = RE_TIME.match(text) if in_header else None
    if m:
        ctx["time"] = m.group(1).strip()
        if ctx["court_no"] in benches and not benches[ctx["court_no"]]["time"]:
            benches[ctx["court_no"]]["time"] = ctx["time"]
        return "skip"

    if up == "SUPPLEMENTARY LIST":
        ctx.update(supplementary=True, drop=False)
        return "section"
    if up in HEARING_HEADINGS:
        ctx["drop"] = False
        return "skip"

    # Section headings are centred; "[R-1], [CAVEAT] …" lines in the
    # Advocate column are AoR text, not headings.
    m = RE_SECTION.match(text) if left_of_advocates else None
    if m and not re.match(r"^\[\s*[PR]-\d", text):
        ctx["section"] = clean(m.group(1) + " " + (m.group(2) or ""))
        return "section"

    if up in SKIP_EXACT or up.startswith(SKIP_PREFIXES) or RE_STAMP.search(text):
        return "skip"
    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def list_label(list_code: Optional[str], kind: str) -> str:
    """'M_J_1' -> 'Miscellaneous — main list'; advance -> 'Advance list (tentative)'."""
    if kind == "advance":
        return "Advance list (tentative)"
    m = re.match(r"^([MF])_([JCSR])_(\d+)$", list_code or "")
    if not m:
        return list_code or "Cause list"
    hearing = {"M": "Miscellaneous", "F": "Regular hearing"}[m.group(1)]
    bench = {"J": "", "C": "Chamber", "S": "Single Judge", "R": "Registrar"}[m.group(2)]
    part = "main list" if m.group(3) == "1" else "supplementary list"
    head = f"{bench} ({hearing.lower()})" if bench and hearing != "Miscellaneous" else (bench or hearing)
    return f"{head} — {part}"


def parse_pdf(data: bytes, list_code: Optional[str] = None, kind: str = "daily") -> dict:
    """
    Parse one cause-list PDF.

    Returns {"list_date", "kind", "list_code", "list_label", "supplementary",
             "benches": [...], "entries": [...], "stats": {...}}.
    """
    ctx = {"list_date": None, "kind": kind, "court_no": None, "section": None, "bench": [], "time": None,
           "supplementary": bool(list_code and list_code.endswith("_2")), "drop": False}
    benches: "OrderedDict[str, dict]" = OrderedDict()
    entries: List[dict] = []
    stats = {"pages": 0, "unknown_types": {}, "no_case_number": 0, "drop_rows_skipped": 0}
    cols = None
    item: Optional[dict] = None

    def close():
        nonlocal item
        if item is not None:
            entries.append(_finish(item, stats))
            item = None

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        stats["pages"] = len(pdf.pages)
        for page in pdf.pages:
            lines = _page_lines(page)
            hdr_idx, found = _find_header(lines)
            if found:
                cols = found
            elif cols is None:
                cols = _default_cols(page.width)

            for i, ln in enumerate(lines):
                if hdr_idx is not None and i == hdr_idx:
                    ctx["drop"] = False
                    continue
                in_header = hdr_idx is not None and i < hdr_idx
                left = ln["words"][0]["x0"] < (cols[3] - 4)
                kind_of_line = _context(ln["text"], ctx, benches, in_header, left)
                if kind_of_line in ("court", "section"):
                    close()
                    continue
                if kind_of_line == "skip":
                    continue
                # Lines above the table header on a page are notes and
                # headings, never item text.
                if in_header:
                    continue
                cells = _cells(ln["words"], cols)
                if ctx["drop"]:
                    stats["drop_rows_skipped"] += 1
                    continue
                m = RE_SNO.match(cells[0]) if cells[0] else None
                if m and _sno_wraps(item, cells[0]):
                    item["sno"] += cells[0]
                    item["sub"] = int(item["sno"].partition(".")[2])
                    item["pending_sub"] = False
                    _feed(item, cells)
                elif m:
                    close()
                    item = _new_item(cells[0], ctx)
                    _feed(item, cells)
                elif item is not None:
                    _feed(item, cells)
            # an item can continue onto the next page; keep it open
        close()

    _apply_only_listed(entries)
    stats["entries"] = len(entries)
    stats["unknown_types"] = dict(sorted(stats["unknown_types"].items(), key=lambda kv: -kv[1]))
    return {
        "list_date": ctx["list_date"],
        "kind": ctx["kind"],
        "list_code": list_code,
        "list_label": list_label(list_code, ctx["kind"]),
        "supplementary": ctx["supplementary"],
        "benches": list(benches.values()),
        "entries": entries,
        "stats": stats,
    }
