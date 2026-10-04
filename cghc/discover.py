"""
Find the Chhattisgarh High Court cause lists that are currently published.

highcourt.cg.gov.in/clists works in two steps, with no CAPTCHA:

    GET  /clists/api/getDate.php?toc=<type>        -> [{"doc": "2026-10-06"}, …]  dates with a list
    POST /clists/courtlist.php  clistgroup=<type>&ct_date=<date>
         -> page with <input id="pdf_location" value="CG05102026.PDF">

and the PDF is /clists/causelists/pdf/<name>. List types (clistgroup):
1 daily, 2 supplementary, 3 weekly (tentative, Mon–Fri), 7 Additional
Registrar. Lok Adalat lists (5, 6) are not read. File names vary in case
("CG05102026.PDF", "CG06102026.pdf"), so they are always taken from the page.
"""

from __future__ import annotations

import logging
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List

import requests

log = logging.getLogger(__name__)

BASE = "https://highcourt.cg.gov.in/clists/"
PDF_BASE = BASE + "causelists/pdf/"
USER_AGENT = "OneCourtPro/1.0 (+https://onecourt.in; cause-list alerts)"
IST = timezone(timedelta(hours=5, minutes=30))

# clistgroup -> (list_code, kind, supplementary, registrar)
GROUPS = {
    1: ("DAILY", "daily", False, False),
    2: ("SUPP", "daily", True, False),
    3: ("WEEKLY", "advance", False, False),
    7: ("AR", "daily", False, True),
}
_LOC_RE = re.compile(r'id="pdf_location"[^>]*value="([^"]*)"|value="([^"]*)"[^>]*id="pdf_location"')
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+\.pdf$", re.I)


# highcourt.cg.gov.in uses Let's Encrypt's newer "ISRG Root YR" (2025), which
# older certificate bundles (certifi, GitHub's runners) don't have yet. We add
# that one official root (letsencrypt.org/certs/gen-y/root-yr.pem, SHA-256
# E5:7B:7E:6F:…:EB:F4:A8:6F) to the usual bundle — certificates are still
# fully checked.
EXTRA_ROOT = Path(__file__).resolve().parent / "certs" / "isrg-root-yr.pem"


def _ca_bundle() -> str:
    import certifi
    out = Path(tempfile.gettempdir()) / "onecourt-cghc-ca.pem"
    if not out.exists():
        out.write_text(Path(certifi.where()).read_text() + "\n" + EXTRA_ROOT.read_text())
    return str(out)


def new_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    s.verify = _ca_bundle()
    return s


def _today() -> date:
    return datetime.now(IST).date()


def list_dates(session: requests.Session, group: int) -> List[str]:
    r = session.get(BASE + "api/getDate.php", params={"toc": group}, timeout=30)
    r.raise_for_status()
    return [d["doc"] for d in (r.json() or []) if isinstance(d, dict) and re.match(r"^\d{4}-\d{2}-\d{2}$", str(d.get("doc", "")))]


def pdf_name(session: requests.Session, group: int, day: str) -> str:
    r = session.post(BASE + "courtlist.php", data={"clistgroup": group, "ct_date": day}, timeout=30)
    r.raise_for_status()
    m = _LOC_RE.search(r.text)
    name = (m.group(1) or m.group(2) or "").strip() if m else ""
    return name if _SAFE_NAME.match(name) else ""


def discover(session: requests.Session, days_back: int = 1) -> List[dict]:
    """Lists for today onwards (and `days_back` days before). Weekly lists from the start of this week."""
    today = _today()
    out: List[dict] = []
    for group, (code, kind, supp, registrar) in GROUPS.items():
        try:
            dates = list_dates(session, group)
        except Exception as exc:  # one list type failing shouldn't stop the others
            log.warning("CGHC: dates for list type %s failed: %s", group, exc)
            continue
        earliest = today - timedelta(days=4 if kind == "advance" else days_back)   # a weekly list runs Mon–Fri
        for day in dates:
            if date.fromisoformat(day) < earliest:
                continue
            name = pdf_name(session, group, day)
            if not name:
                continue
            out.append({"url": PDF_BASE + name, "list_date": day, "list_code": code, "kind": kind,
                        "supplementary": supp, "registrar": registrar, "name": name})
            if supp:
                # The page names the first supplementary list; later ones follow the pattern.
                stem = re.sub(r"-SUP1\.pdf$", "", name, flags=re.I)
                for n in range(2, 6):
                    hit = next((stem + f"-SUP{n}" + ext for ext in (".PDF", ".pdf")
                                if session.head(PDF_BASE + stem + f"-SUP{n}" + ext, timeout=20).status_code == 200), None)
                    if not hit:
                        break
                    out.append({"url": PDF_BASE + hit, "list_date": day, "list_code": f"SUPP{n}", "kind": kind,
                                "supplementary": True, "registrar": False, "name": hit})
    return out
