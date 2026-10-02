"""
Turn case numbers as printed in cause lists into canonical keys.

    "SLP(Crl) No. 16213/2026"                    -> SLPCRL|16213|2026
    "Diary No. 48235-2026"                       -> DIARY|48235|2026
    "SLP(C) No. 17364-17367/2012"                -> SLPC|17364|2012 … SLPC|17367|2012
    "MA 1728/2022 in SLP(Crl) No. 1399/2020"     -> MA|1728|2022 (primary), SLPCRL|1399|2020 (related)

A key is "<TYPE CODE>|<number>|<year>". The app builds the same key from the
type/number/year a user types in, so the two sides meet without any fuzzy
matching. Type codes live in case_types_sci.json.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Tuple

TYPES_FILE = Path(__file__).with_name("case_types_sci.json")

# Longest range we are willing to expand ("C.A. No. 20-21/2024" is 2 cases;
# anything far bigger is almost certainly a misread).
MAX_RANGE = 200

CASE_REF_RE = re.compile(
    r"(?P<type>[A-Za-z][A-Za-z.()'\s…]*?)"      # type words, e.g. "SLP(Crl)", "C.A.", "MA"
    r"\s*(?:No\(s\)|Nos?)?\.?\s*"                 # optional "No." / "Nos." / "No(s)."
    r"(?P<num>\d+[A-Z]?(?:\s*-\s*\d+[A-Z]?)?)"     # number or range: "17364-17367", "33782-33837A"
    r"\s*(?P<sep>[/-])\s*"
    r"(?P<year>(?:19|20)\d{2})(?!\d)"
)

# Reference types whose names contain digits, which the type pattern can't
# hold; rewritten to a digit-free token (an alias in case_types_sci.json).
_SPECIAL_TYPES = [
    (re.compile(r"REF\.?\s*U/A\s*317\s*\(\s*1\s*\)", re.I), "REFARTICLE"),
    (re.compile(r"REF\.?\s*U/S\s*14\s*RTI", re.I), "REFRTIFOURTEEN"),
    (re.compile(r"REF\.?\s*U/S\s*17\s*RTI", re.I), "REFRTISEVENTEEN"),
]

# Words that join or decorate case numbers rather than name a case type.
_CONNECTORS_RE = re.compile(r"\b(?:in|and|with|connected|pil|only)\b", re.I)


def clean(text: str) -> str:
    """Collapse runs of whitespace."""
    return re.sub(r"\s+", " ", text or "").strip()


def alias_key(type_text: str) -> str:
    """'S.L.P.(C)...CC' -> 'SLP(C)CC'; 'T.P.(Crl.)' -> 'TP(CRL)'."""
    t = (type_text or "").upper().replace("…", "")
    return re.sub(r"[\s.]", "", t)


@lru_cache(maxsize=1)
def case_types() -> Dict[str, dict]:
    """code -> type record."""
    data = json.loads(TYPES_FILE.read_text(encoding="utf-8"))
    return {t["code"]: t for t in data["types"]}


@lru_cache(maxsize=1)
def _alias_index() -> Dict[str, str]:
    index: Dict[str, str] = {}
    for code, t in case_types().items():
        for a in t["aliases"]:
            index[alias_key(a)] = code
    return index


def type_code(type_text: str):
    """Case type as printed -> canonical code, or None if unknown."""
    return _alias_index().get(alias_key(type_text))


def norm_number(number) -> str:
    """'00123' -> '123', '33837a' -> '33837A'. Case numbers can carry a letter suffix."""
    m = re.match(r"^\s*0*(\d+)\s*([A-Za-z]?)\s*$", str(number))
    if not m:
        raise ValueError(f"not a case number: {number!r}")
    return m.group(1) + m.group(2).upper()


def make_key(code: str, number, year) -> str:
    return f"{code}|{norm_number(number)}|{int(year)}"


def _clean_type(raw: str) -> str:
    """Keep only the last type phrase: 'W Connected MA' -> 'MA', 'in SLP(Crl)' -> 'SLP(Crl)'."""
    parts = [p.strip() for p in _CONNECTORS_RE.split(raw) if p and p.strip()]
    last = parts[-1] if parts else raw.strip()
    # The "W" left over from a "PIL-W" tag ("-" can't be part of a type, so a
    # match may start at that W): "W MA" -> "MA". "W.P.(C)" is untouched.
    return re.sub(r"^W\s+", "", last)


def _expand(num_text: str) -> List[str]:
    """
    '17364-17367' -> 17364 … 17367;  '5031' -> 5031;  '1234-36' -> 1234 … 1236;
    '33782-33837A' -> 33782 … 33837 plus 33837A.
    """
    bits = [b.strip() for b in num_text.split("-")]
    m_start = re.match(r"^(\d+)([A-Z]?)$", bits[0])
    start, start_suffix = int(m_start.group(1)), m_start.group(2)
    if len(bits) == 1 or not bits[1]:
        return [f"{start}{start_suffix}"]
    m_end = re.match(r"^(\d+)([A-Z]?)$", bits[1])
    end_txt, end_suffix = m_end.group(1), m_end.group(2)
    end = int(end_txt)
    if end < start and len(end_txt) < len(m_start.group(1)):
        # abbreviated end, e.g. 1234-36 -> 1236
        end = int(m_start.group(1)[: len(m_start.group(1)) - len(end_txt)] + end_txt)
    if end < start or end - start > MAX_RANGE:
        return [f"{start}{start_suffix}"]
    out = [str(n) for n in range(start, end + 1)]
    if start_suffix:
        out.insert(0, f"{start}{start_suffix}")
    if end_suffix:
        out.append(f"{end}{end_suffix}")
    return out


def parse_case_numbers(text: str) -> Tuple[List[dict], List[str]]:
    """
    Find every case number in a cause-list "Case No." cell.

    Returns (refs, unknown_types). Each ref is
      {"key", "code", "number" (str), "year", "role"}  with role "primary" for the
    case the item is about and "related" for the rest (parent cases after
    "in", extra cases in the same item).
    """
    refs: List[dict] = []
    unknown: List[str] = []
    seen = set()
    text = clean(text)
    for pattern, token in _SPECIAL_TYPES:
        text = pattern.sub(token, text)
    for m in CASE_REF_RE.finditer(text):
        type_text = _clean_type(m.group("type"))
        code = type_code(type_text)
        if code is None:
            unknown.append(type_text)
            continue
        year = int(m.group("year"))
        if not 1950 <= year <= 2100:
            continue
        role = "primary" if not refs else "related"
        for n in _expand(m.group("num")):
            key = make_key(code, n, year)
            if key in seen:
                continue
            seen.add(key)
            refs.append({"key": key, "code": code, "number": n, "year": year, "role": role})
    return refs, unknown


_BRACKETS_RE = re.compile(r"\[[^\]]*\]")
# "NAME- 2212". Not followed by another "-": "ADITYA JAIN-1- 2985" is code 2985, not 1.
_AOR_CODE_RE = re.compile(r"-\s*(\d{1,5})(?![\d-])")


def extract_aor_codes(text: str) -> List[int]:
    """
    AoR codes from the Advocate column: 'RAJ KISHOR CHOUDHARY- 2212' -> [2212].
    Party markers such as [R-1], [P-2], [CAVEAT] are removed first.
    """
    t = _BRACKETS_RE.sub(" ", text or "")
    return sorted({int(c) for c in _AOR_CODE_RE.findall(t)})
