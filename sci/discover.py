"""
Find the Supreme Court cause-list PDFs that are currently published.

The lists live on SCI's public file server at predictable addresses:

    https://api.sci.gov.in/jonew/cl/<YYYY-MM-DD>/<CODE>.pdf          daily lists
    https://api.sci.gov.in/jonew/cl/advance/<YYYY-MM-DD>/M_J.pdf     advance lists

CODE is Misc/Regular (M/F) × Judges/Chamber/Single judge/Registrar (J/C/S/R)
× main/supplementary (1/2), e.g. M_J_1, M_R_2.

We ask the file server directly (cheap HEAD requests) rather than reading the
www.sci.gov.in cause-list page: that site refuses clients that identify as a
bot, and we identify honestly rather than pretend to be a browser.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional, Tuple

import requests

log = logging.getLogger(__name__)

PDF_BASE = "https://api.sci.gov.in/jonew/cl"
USER_AGENT = "Mozilla/5.0 (compatible; OneCourtBot/1.0; +https://app.onecourt.in)"

# Main lists; each one's supplementary list ("_2") is checked only if the main exists.
MAIN_CODES = ["M_J_1", "M_C_1", "M_S_1", "M_R_1", "F_J_1"]
# Daily lists appear 1-3 working days ahead; 7 calendar days covers long
# weekends and holidays. Advance lists run about two weeks ahead.
DAILY_DAYS = 7
ADVANCE_DAYS = 16
IST = timezone(timedelta(hours=5, minutes=30))


def ist_today() -> date:
    return datetime.now(IST).date()


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def pdf_url(list_date: str, code: str, kind: str = "daily") -> str:
    if kind == "advance":
        return f"{PDF_BASE}/advance/{list_date}/{code}.pdf"
    return f"{PDF_BASE}/{list_date}/{code}.pdf"


def _exists(session: requests.Session, url: str) -> bool:
    try:
        return session.head(url, timeout=15, allow_redirects=True).status_code == 200
    except requests.RequestException as exc:
        log.warning("HEAD %s failed: %s", url, exc)
        return False


def discover(session: requests.Session, today: Optional[date] = None) -> Tuple[List[dict], str]:
    """Return (pdfs, source) for every list dated today or later."""
    today = today or ist_today()
    found: List[dict] = []
    for d in range(DAILY_DAYS):
        day = (today + timedelta(days=d)).isoformat()
        for main in MAIN_CODES:
            if not _exists(session, pdf_url(day, main)):
                continue
            found.append({"url": pdf_url(day, main), "kind": "daily", "list_date": day, "list_code": main})
            supp = main[:-1] + "2"
            if _exists(session, pdf_url(day, supp)):
                found.append({"url": pdf_url(day, supp), "kind": "daily", "list_date": day, "list_code": supp})
    for d in range(ADVANCE_DAYS):
        day = (today + timedelta(days=d)).isoformat()
        url = pdf_url(day, "M_J", "advance")
        if _exists(session, url):
            found.append({"url": url, "kind": "advance", "list_date": day, "list_code": "M_J"})
    # daily lists first (they matter most for tomorrow), nearest date first
    found.sort(key=lambda p: (0 if p["kind"] == "daily" else 1, p["list_date"], p["list_code"]))
    return found, "probe"
