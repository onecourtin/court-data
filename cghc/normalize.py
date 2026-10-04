"""
Turn Chhattisgarh High Court case numbers, as printed in its cause lists,
into keys.

    "WPS/4131/2026"              -> CGWPS|4131|2026
    "FA(MAT)/376/2025"           -> CGFAMAT|376|2025
    "WITH WA 661/2026 & WPS 4131/2026"  (remarks)  -> CGWA|661|2026, CGWPS|4131|2026

Codes are "CG" + the type with everything but letters and digits removed
(common/case_types_cghc.json), so they never collide with other courts.
Filing numbers of not-yet-registered cases ("List Of Defective Cases") use
the same TYPE/number/year shape but a different series; the parser never
reads that section as listings.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from common.normalize import clean, make_key

TYPES_FILE = Path(__file__).resolve().parent.parent / "common" / "case_types_cghc.json"

# The item's own number: TYPE/NUMBER/YEAR (no spaces).
CASE_RE = re.compile(r"^(?P<type>[A-Z][A-Z0-9().]*?)/(?P<num>\d{1,7})/(?P<year>(?:19|20)\d{2})$")
# Numbers mentioned in remarks: "WPCR 398/2026", "WPS NO. 691/2021", "CRMP NO. 2153/2026".
REF_RE = re.compile(r"\b(?P<type>[A-Z][A-Z0-9().]{0,10}?)\s*(?:NOS?\.?\s*)?(?P<num>\d{1,7})\s*/\s*(?P<year>(?:19|20)\d{2})\b")


def compact(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (text or "").upper())


@lru_cache(maxsize=1)
def case_types() -> Dict[str, dict]:
    data = json.loads(TYPES_FILE.read_text(encoding="utf-8"))
    return {t["code"]: t for t in data["types"]}


@lru_cache(maxsize=1)
def _alias_index() -> Dict[str, str]:
    index: Dict[str, str] = {}
    for code, t in case_types().items():
        for a in t["aliases"] + [t["label"], code[2:]]:
            index.setdefault(compact(a), code)
    return index


def type_code(type_text: str) -> Optional[str]:
    return _alias_index().get(compact(type_text))


def parse_case_token(token: str) -> Tuple[Optional[dict], Optional[str]]:
    """'WPS/4131/2026' -> ({key, code, number, year}, None); unknown type -> (None, 'XYZ')."""
    m = CASE_RE.match((token or "").strip().upper())
    if not m:
        return None, None
    code = type_code(m.group("type"))
    if code is None:
        return None, m.group("type")
    return {"key": make_key(code, m.group("num"), m.group("year")), "code": code,
            "number": str(int(m.group("num"))), "year": int(m.group("year"))}, None


def remark_refs(text: str) -> List[str]:
    """Keys of the cases named in a remark's "WITH …" part (connected matters)."""
    keys: List[str] = []
    for part in re.findall(r"\bWITH\b([^\]\)]*)", clean(text).upper()):
        for m in REF_RE.finditer(part):
            code = type_code(m.group("type"))
            if code:
                k = make_key(code, m.group("num"), m.group("year"))
                if k not in keys:
                    keys.append(k)
    return keys
