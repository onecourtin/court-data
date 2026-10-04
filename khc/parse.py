"""
Read a Karnataka High Court consolidated cause list
(judiciary.karnataka.gov.in/pdfs/consolidatedCauselist/{blr,dwd,klb}consolidation.pdf):
one PDF per bench (Bengaluru, Dharwad, Kalaburagi) holding the next sitting
day's lists for every court hall.

Each block starts with

    IN THE HIGH COURT OF KARNATAKA AT BENGALURU
    ON THE DAY OF Monday THE 5th Day Of October 2026
    at 10:30AM
    BEFORE
    THE HON'BLE CHIEF JUSTICE  &  THE HON'BLE MRS. JUSTICE K.S. HEMALEKHA   (or JOINT REGISTRAR 1)
    COURT HALL NO :1
    Cause List No. 1
    Sl. No. | Case No. | Pet/Appl/Comp. & Adv. | Resp. & Adv.

and continues on later pages under a "COURT HALL NO:1" line. Items:

    1    CRP 859/2026 (,)    PET: SMT. AMRITA BANERJEE   RES: SRI MUNIRAJU T AND OTHERS
                             NIKHIL K  (bold = advocate)
    1 .1 CCC 796/2025 (,)    … (connected matter)

Party names are printed in italics, advocates in bold, which is how the two
are told apart. Case numbers: "WP 30555/2026", "CRL.A 1326/2026" → keys
"K" + type letters: KWP|30555|2026, KCRLA|1326|2026. (Karnataka gives each
bench its own number range, so one key per case is enough.)
"""

from __future__ import annotations

import io
import re
from datetime import date
from typing import Dict, List, Optional

import pdfplumber

from common.normalize import clean, make_key

PARSER_VERSION = "khc-1"
BENCH_CODE = {"BENGALURU": "B", "DHARWAD": "D", "KALABURAGI": "K", "KALBURAGI": "K", "GULBARGA": "K"}
_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"], 1)}
_CASE_RE = re.compile(r"^(?P<type>[A-Z][A-Z0-9.()&/-]*(?:\s[A-Z][A-Z0-9.()&-]*)?)\s+(?P<num>\d{1,7})/(?P<year>(?:19|20)\d{2})\b")
_HALL_RE = re.compile(r"COURT\s*HALL\s*NO\s*:?\s*([0-9]+[A-Z]?)", re.I)
_DAY_RE = re.compile(r"THE\s+(\d{1,2})(?:st|nd|rd|th)?\s+Day\s+Of\s+([A-Za-z]+)\s+(\d{4})", re.I)


def type_code(type_text: str) -> str:
    return "K" + re.sub(r"[^A-Z0-9]", "", type_text.upper())


def _lines(page) -> List[List[dict]]:
    words = page.extract_words(x_tolerance=1.5, y_tolerance=2, extra_attrs=["fontname"])
    rows: Dict[int, List[dict]] = {}
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        top = round(w["top"])
        key = next((k for k in rows if abs(k - top) <= 2), top)
        rows.setdefault(key, []).append(w)
    return [sorted(rows[k], key=lambda w: w["x0"]) for k in sorted(rows)]


def _judge(text: str) -> str:
    t = re.sub(r"^THE\s+HON'?BLE\s+", "", text.strip(), flags=re.I)
    if re.match(r"CHIEF JUSTICE", t, re.I):
        return "The Chief Justice"
    t = re.sub(r"^(MR|MRS|MS|DR|SMT|SRI|SHRI)\.?\s+", "", t, flags=re.I)
    return " ".join(p if "." in p and len(p) <= 12 else p.capitalize() for p in t.split())


def _party(text: str) -> str:
    """The party name only: stop after "AND OTHERS" / "AND ANOTHER" (notes about counsel follow it)."""
    t = clean(text)
    m = re.search(r"\bAND\s+(?:OTHERS|ANOTHER|ORS\.?)(?=\s|$)", t)
    return t[:m.end()] if m else t


def parse_pdf(data: bytes, bench: str = "B") -> dict:
    stats = {"pages": 0, "entries": 0, "blocks": 0, "no_case_number": 0, "unknown_types": {}}
    entries: List[dict] = []
    dates: Dict[str, int] = {}
    cols = {"case": 70.0, "cat": 200.0, "pet": 292.0, "res": 430.0}
    block = {"judges": [], "hall": None, "time": None, "list": None}
    header = False
    section: Optional[str] = None
    item: Optional[dict] = None
    connected_next = False

    def finish():
        nonlocal item
        if not item:
            return
        m = _CASE_RE.match(clean(item["case"][0]) if item["case"] else "")
        if not m:
            stats["no_case_number"] += 1
            item = None
            return
        code = type_code(m.group("type"))
        key = make_key(code, m.group("num"), m.group("year"))
        pet_party = clean(" ".join(item["pet_party"])).removeprefix("PET:").strip()
        res_party = clean(" ".join(item["res_party"])).removeprefix("RES:").strip()
        sect = " · ".join(x for x in [f"List {block['list']}" if block["list"] else "", section or ""] if x)
        entries.append({
            "court_no": f"K{bench}{block['hall']}" if block["hall"] else None,
            "item_no": item["sno"],
            "item_main": int(item["sno"].split(".")[0]),
            "item_sub": int(item["sno"].split(".")[1]) if "." in item["sno"] else 0,
            "connected": item["connected"],
            "case_no_raw": f"{m.group('type')} {m.group('num')}/{m.group('year')}",
            "keys": [key],
            "primary_key": key,
            "type_text": m.group("type"),
            "petitioner": _party(pet_party),
            "respondent": _party(res_party),
            "petitioner_aor": ", ".join(clean(x) for x in item["pet_adv"] if clean(x)),
            "respondent_aor": ", ".join(clean(x) for x in item["res_adv"] if clean(x)),
            "aor_codes": [],
            "section": sect or None,
            "judges": list(block["judges"]),
            "time": block["time"],
            "listed": True,
            "note": clean(" ".join(item["case"][1:]))[:300] or None,
        })
        item = None

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        for page in pdf.pages:
            stats["pages"] += 1
            for line in _lines(page):
                text = clean(" ".join(w["text"] for w in line))
                up = text.upper()
                x0 = line[0]["x0"]
                if up.startswith("WEBSITE:") or up.startswith("(TO GET DAILY") or up.startswith("CLICKING:"):
                    continue
                if up.startswith("IN THE HIGH COURT OF KARNATAKA"):
                    finish()
                    stats["blocks"] += 1
                    block = {"judges": [], "hall": None, "time": None, "list": None}
                    header, section, connected_next = True, None, False
                    continue
                if header:
                    d = _DAY_RE.search(text)
                    if d and d.group(2).lower() in _MONTHS:
                        iso = date(int(d.group(3)), _MONTHS[d.group(2).lower()], int(d.group(1))).isoformat()
                        dates[iso] = dates.get(iso, 0) + 1
                    elif re.match(r"^at\s+\d", text, re.I):
                        block["time"] = text[3:].strip()
                    elif up.startswith("THE HON") or "REGISTRAR" in up:
                        block["judges"].append(_judge(text) if up.startswith("THE HON") else text.title())
                    elif _HALL_RE.search(text):
                        block["hall"] = _HALL_RE.search(text).group(1).upper()
                    elif up.startswith("CAUSE LIST NO"):
                        block["list"] = re.sub(r"\D", "", text) or None
                    elif up.startswith("SL."):
                        xs = {w["text"].upper(): w["x0"] for w in line}
                        cols = {"case": xs.get("CASE", 78) - 6, "cat": 200.0,
                                "pet": xs.get("PET/APPL/COMP.", 298) - 6, "res": xs.get("RESP.", 435) - 6}
                        header = False
                    continue
                if _HALL_RE.match(text) or up.startswith("SL.") or up.startswith("NO. &"):
                    continue          # page heading on continuation pages
                if up.startswith("CONNECTED WITH"):
                    connected_next = True
                    continue
                sno_words = [w["text"] for w in line if w["x0"] < cols["case"]]
                case_words = [w for w in line if cols["case"] <= w["x0"] < cols["pet"]]
                case_text = clean(" ".join(w["text"] for w in case_words if w["x0"] < cols["cat"]))
                sno = "".join(sno_words).replace(" ", "")
                if re.fullmatch(r"\d{1,4}(\.\d{1,3})?", sno) and _CASE_RE.match(case_text):
                    finish()
                    res_x = next((w["x0"] for w in line if w["text"].upper() == "RES:"), cols["res"] + 6)
                    item = {"sno": sno, "case": [case_text], "pet_party": [], "pet_adv": [], "res_party": [], "res_adv": [],
                            "connected": connected_next or "." in sno, "res_x": res_x - 3}
                    connected_next = False
                elif not item:
                    if x0 > 120 and not sno_words and re.fullmatch(r"[A-Z0-9 ./&()'-]+", up) and len(up) < 90:
                        section = text.title()
                    continue
                elif case_text:
                    item["case"].append(case_text)
                elif x0 > 120 and not [w for w in line if w["x0"] >= cols["pet"]] and re.fullmatch(r"[A-Z0-9 ./&()'-]+", up):
                    finish()
                    section = text.title()
                    continue
                for col, lo, hi in (("pet", cols["pet"], item["res_x"]), ("res", item["res_x"], 10_000)):
                    ws = [w for w in line if lo <= w["x0"] < hi]
                    if not ws:
                        continue
                    bold = [w["text"] for w in ws if "Bold" in w["fontname"]]
                    rest = [w["text"] for w in ws if "Bold" not in w["fontname"]]
                    if rest:
                        item[f"{col}_party"].append(" ".join(rest))
                    if bold:
                        item[f"{col}_adv"].append(" ".join(bold))
        finish()

    stats["entries"] = len(entries)
    return {"list_date": max(dates, key=dates.get) if dates else None, "kind": "daily",
            "list_label": "Daily list", "supplementary": False, "benches": [], "entries": entries, "stats": stats}
