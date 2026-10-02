"""
Find the NCLAT cause-list PDFs that are currently published.

NCLAT lists every PDF on one page, https://nclat.nic.in/daily-cause-list,
as /sites/default/files/<YYYY-MM>/<file>.pdf. The court is only in the file
name (seen 2 Oct 2026):

    Causelist_ch_05.10.2026.pdf        Court of Chairperson (Court I)
    Causelist_II_05.10.2026.pdf        Court II  (III, IV likewise)
    Causelist_IV_06.10.2026_0.pdf      "_0", "_1": a corrected re-upload
    Supp_Causelist_ch_01.10.2026.pdf   supplementary list
    Registrar Court_01.10.2026.pdf     Registrar Court
    05.10.2026.pdf                     Chennai Bench (bare date)
    06.10.2026_compressed.pdf          Chennai Bench
    05.10.2026 Suppl.pdf               Chennai Bench, supplementary

Files whose court can't be told are reported, not silently dropped.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote, urljoin

import requests

log = logging.getLogger(__name__)

PAGE_URL = "https://nclat.nic.in/daily-cause-list"
USER_AGENT = "Mozilla/5.0 (compatible; OneCourtBot/1.0; +https://app.onecourt.in)"
IST = timezone(timedelta(hours=5, minutes=30))

# file-name pattern -> (list code, court_no as the app stores it, label).
# NCLAT court numbers start with N so the app never confuses them with SCI
# courts ("1" is SCI Court 1, "N1" the NCLAT Court of Chairperson).
# Order matters: IV before II, III before II.
COURTS: List[Tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"(?:^|_)ch(?:_|$)", re.I), "COURT1", "N1", "Court of Chairperson"),
    (re.compile(r"(?:^|_)iv(?:_|$)", re.I), "COURT4", "N4", "Court IV"),
    (re.compile(r"(?:^|_)iii(?:_|$)", re.I), "COURT3", "N3", "Court III"),
    (re.compile(r"(?:^|_)ii(?:_|$)", re.I), "COURT2", "N2", "Court II"),
    (re.compile(r"registrar", re.I), "REGISTRAR", "NR", "Registrar Court"),
]
CHENNAI = ("CHENNAI", "NCH", "Chennai Bench")

_DATE_RE = re.compile(r"(\d{2})[._-](\d{2})[._-](\d{4})")
_SUPP_RE = re.compile(r"supp", re.I)
_VERSION_RE = re.compile(r"_(\d+)$")


def ist_today() -> date:
    return datetime.now(IST).date()


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hrefs: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href") or ""
            if href.lower().endswith(".pdf"):
                self.hrefs.append(href)


def classify(filename: str) -> Optional[dict]:
    """
    'Causelist_IV_06.10.2026_0.pdf' -> {list_date, list_code 'COURT4', court_no 'N4',
    label, supplementary, version 0}; None if it isn't a dated cause list.
    """
    name = unquote(filename)
    stem = re.sub(r"\.pdf$", "", name, flags=re.I).strip()
    m = _DATE_RE.search(stem)
    if not m:
        return None
    try:
        list_date = date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None
    supplementary = bool(_SUPP_RE.search(stem))
    vm = _VERSION_RE.search(stem)
    version = int(vm.group(1)) if vm else -1

    before, after = stem[: m.start()], stem[m.end():]
    court = None
    for pattern, code, court_no, label in COURTS:
        if pattern.search(before.replace(" ", "_")):
            court = (code, court_no, label)
            break
    if court is None:
        # Chennai files are just the date, maybe with "Suppl", "_compressed" or "_0".
        rest = re.sub(r"supp\w*\.?|compressed|_\d+|[\s_().-]", "", before + after, flags=re.I)
        if rest:
            return None
        court = CHENNAI
    code, court_no, label = court
    return {
        "list_date": list_date,
        "list_code": code + ("_S" if supplementary else ""),
        "court_no": court_no,
        "court_label": label,
        "bench": "chennai" if code == "CHENNAI" else "delhi",
        "supplementary": supplementary,
        "version": version,
    }


def discover(session: requests.Session, today: Optional[date] = None) -> Tuple[List[dict], List[str]]:
    """
    Return (pdfs, unrecognised) for lists dated today or later. When a list was
    re-uploaded ("_0", "_1"), only the newest copy is kept.
    """
    today = (today or ist_today()).isoformat()
    resp = session.get(PAGE_URL, timeout=30)
    resp.raise_for_status()
    parser = _Links()
    parser.feed(resp.text)

    best: Dict[Tuple[str, str], dict] = {}
    unknown: List[str] = []
    for href in dict.fromkeys(parser.hrefs):   # de-duplicate, keep order
        filename = href.rsplit("/", 1)[-1]
        info = classify(filename)
        if info is None:
            if _DATE_RE.search(unquote(filename)):
                unknown.append(unquote(filename))
            continue
        if info["list_date"] < today:
            continue
        info["url"] = urljoin(PAGE_URL, href)
        info["kind"] = "daily"
        slot = (info["list_date"], info["list_code"])
        if slot not in best or info["version"] > best[slot]["version"]:
            best[slot] = info
    pdfs = sorted(best.values(), key=lambda p: (p["list_date"], p["list_code"]))
    return pdfs, unknown
