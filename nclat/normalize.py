"""
Turn NCLAT case numbers, as printed in its cause lists, into keys.

    "Comp. App. (AT) (Ins) No. 1785 of 2025"            -> NCAI|1785|2025
    "Comp App (AT) (CH) (Ins) No. 510/2026"             -> NCAICH|510|2026   (Chennai Bench series)
    "Comp. App. (AT) (Ins) No. 1284 & 1285 of 2026"     -> NCAI|1284|2026, NCAI|1285|2026
    "I.A. No. 7331, 7332 of 2025"                       -> NIA|7331|2025, NIA|7332|2025
    "I.A. No. 6995 of 2026 in Comp. App. (AT) (Ins) No. 1125 of 2026"
                                                        -> NIA|6995|2026 (primary), NCAI|1125|2026

Keys look like SCI keys ("<CODE>|<number>|<year>"); NCLAT codes start with N
(case_types_nclat.json), so the app can hold both courts in one table.
The Chennai Bench numbers its cases separately, so its keys end in CH.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from common.normalize import clean, make_key

TYPES_FILE = Path(__file__).resolve().parent.parent / "common" / "case_types_nclat.json"
MAX_RANGE = 50

# "<type words> No(s). <numbers> of|/ <year>". The type is read back from the
# words before "No." (see _type_from_words), so it can be loose here.
_REF_RE = re.compile(
    r"(?P<before>[^;]*?)"
    r"\bNos?\s*\.?\s*(?:\(s\)\s*\.?\s*)?"
    r"(?P<nums>\d{1,6}[A-Z]?(?:\s*(?:,|&|and|to|-)\s*\d{1,6}[A-Z]?)*)"
    r"\s*(?:of|/|-)\s*(?P<year>(?:19|20)\d{2})(?!\d)",
    re.I,
)
# Words, dotted abbreviations and bracketed groups: "Comp.", "App.", "(AT)", "(Ins)"
_TOKEN_RE = re.compile(r"\([^()]*\)|[A-Za-z][A-Za-z.']*")
_CH_RE = re.compile(r"^\(\s*CH\s*\)$", re.I)


def alias_key(text: str) -> str:
    """'Comp. App. (AT) (Ins)' and 'Comp. App. AT (Ins)' -> 'COMPAPPATINS'."""
    return re.sub(r"[\s.'()]", "", (text or "").upper())


@lru_cache(maxsize=1)
def case_types() -> Dict[str, dict]:
    data = json.loads(TYPES_FILE.read_text(encoding="utf-8"))
    return {t["code"]: t for t in data["types"]}


@lru_cache(maxsize=1)
def _alias_index() -> Dict[str, str]:
    index: Dict[str, str] = {}
    for code, t in case_types().items():
        for a in t["aliases"] + [t["label"], t["short"]]:
            index[alias_key(a)] = code
    return index


def _type_from_words(before: str) -> Tuple[Optional[str], bool, str]:
    """
    The case type that ends right before "No.": the longest run of trailing
    tokens that is a known alias. Returns (code, chennai, text_seen).
    "(CH)" anywhere in that run marks the Chennai Bench and is dropped.
    """
    tokens = _TOKEN_RE.findall(before)[-8:]
    index = _alias_index()
    for start in range(len(tokens)):
        run = tokens[start:]
        chennai = any(_CH_RE.match(t) for t in run)
        kept = [t for t in run if not _CH_RE.match(t)]
        code = index.get(alias_key(" ".join(kept)))
        if code:
            return code, chennai, " ".join(run)
    return None, False, " ".join(tokens[-4:])


def _expand(nums: str) -> List[str]:
    """'1284 & 1285' -> both; '7331, 7332' -> both; '20-22' / '20 to 22' -> 20, 21, 22."""
    out: List[str] = []
    parts = re.split(r"\s*(,|&|\band\b|\bto\b|-)\s*", nums.strip(), flags=re.I)
    numbers = parts[0::2]
    seps = [s.lower() for s in parts[1::2]]
    for i, n in enumerate(numbers):
        if not n:
            continue
        if i > 0 and seps[i - 1] in ("-", "to") and out:
            start, end = int(re.match(r"\d+", out[-1]).group()), int(re.match(r"\d+", n).group())
            if start < end <= start + MAX_RANGE:
                out += [str(x) for x in range(start + 1, end + 1)]
                continue
        out.append(n.upper())
    return out


def parse_case_numbers(text: str, chennai_list: bool = False) -> Tuple[List[dict], List[str]]:
    """
    Every case number in a cause-list "Case No." cell.
    Returns (refs, unknown_types); a ref is {"key", "code", "number", "year",
    "role", "chennai"}, role "primary" for the first case, "related" after.
    In a Chennai Bench list every number is a Chennai number.
    """
    refs: List[dict] = []
    unknown: List[str] = []
    seen = set()
    text = clean(re.sub(r"[\u2013\u2014]", "-", text or ""))
    for m in _REF_RE.finditer(text):
        code, chennai, seen_text = _type_from_words(m.group("before"))
        if code is None:
            if seen_text:
                unknown.append(seen_text)
            continue
        chennai = chennai or chennai_list
        full = code + ("CH" if chennai else "")
        year = int(m.group("year"))
        for n in _expand(m.group("nums")):
            key = make_key(full, n, year)
            if key in seen:
                continue
            seen.add(key)
            refs.append({"key": key, "code": full, "number": n, "year": year,
                         "role": "primary" if not refs else "related", "chennai": chennai})
    return refs, unknown
