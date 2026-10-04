#!/usr/bin/env python3
"""
Fetch published cause lists (Supreme Court, or NCLAT with --court nclat),
parse them, and send them to the OneCourt app.

    python3 run.py --dry-run                      # parse only, print a summary
    python3 run.py --dry-run --date 2026-10-05    # one date
    python3 run.py --dry-run --out out/           # also save the JSON
    python3 run.py                                # send to $APP_INGEST_URL
    python3 run.py --app http://localhost:8780    # send to a local copy of the app
    python3 run.py --court nclat --dry-run        # NCLAT (Delhi + Chennai benches)
    python3 run.py --court dhc --dry-run          # Delhi High Court
    python3 run.py --court cghc --dry-run         # Chhattisgarh High Court

Only lists that changed since the app last saw them (by ETag) are downloaded
again, so frequent runs stay light on sci.gov.in.

Environment: APP_INGEST_URL, APP_INGEST_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ingest_client import AppClient
from sci.discover import discover, new_session
from sci.parse import parse_pdf

PARSER_VERSION = "sci-1"
NCLAT_PARSER_VERSION = "nclat-1"
DHC_PARSER_VERSION = "dhc-1"
CGHC_PARSER_VERSION = "cghc-1"
KHC_PARSER_VERSION = "khc-1"
log = logging.getLogger("court-data")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--app", default=os.environ.get("APP_INGEST_URL", ""), help="app base URL")
    ap.add_argument("--token", default=os.environ.get("APP_INGEST_TOKEN", ""))
    ap.add_argument("--dry-run", action="store_true", help="parse but don't send")
    ap.add_argument("--date", help="only lists for this date (YYYY-MM-DD)")
    ap.add_argument("--only", help="only this list code, e.g. M_J_1 or M_J (advance)")
    ap.add_argument("--force", action="store_true", help="re-send even if unchanged")
    ap.add_argument("--out", help="directory to write parsed JSON into")
    ap.add_argument("--max", type=int, default=0, help="stop after N lists (testing)")
    ap.add_argument("--court", choices=["sci", "nclat", "dhc", "cghc", "khc"], default="sci")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.court == "nclat":
        return run_nclat(args)
    if args.court == "dhc":
        return run_dhc(args)
    if args.court == "cghc":
        return run_cghc(args)
    if args.court == "khc":
        return run_khc(args)
    session = new_session()
    client = None if args.dry_run else AppClient(args.app, args.token)

    pdfs, source = discover(session)
    if args.date:
        pdfs = [p for p in pdfs if p["list_date"] == args.date]
    if args.only:
        pdfs = [p for p in pdfs if p["list_code"] == args.only]
    log.info("Found %d lists (via %s)", len(pdfs), source)

    known = {} if (client is None or args.force) else client.state("SCI")
    summary = {"started_at": datetime.now(timezone.utc).isoformat(), "source": source,
               "found": len(pdfs), "sent": 0, "unchanged": 0, "errors": []}
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    done = 0
    for p in pdfs:
        if args.max and done >= args.max:
            break
        url = p["url"]
        try:
            head = session.head(url, timeout=20, allow_redirects=True)
            if head.status_code != 200:
                continue
            etag = head.headers.get("ETag", "")
            if etag and known.get(url) == etag:
                summary["unchanged"] += 1
                continue

            resp = session.get(url, timeout=120)
            resp.raise_for_status()
            parsed = parse_pdf(resp.content, list_code=p["list_code"], kind=p["kind"])
            done += 1
            n = len(parsed["entries"])
            if n == 0:
                # Never send an empty list: the app replaces a list's entries
                # on every send, so this would wipe good data. Most likely the
                # PDF layout changed and the parser needs attention.
                raise RuntimeError("parsed 0 entries — parser needs attention")
            if parsed["list_date"] and parsed["list_date"] != p["list_date"]:
                log.warning("%s: header says %s, URL says %s (using URL)", url, parsed["list_date"], p["list_date"])

            payload = {
                "court": "SCI",
                "parser": PARSER_VERSION,
                "pdf_url": url,
                "etag": etag,
                "last_modified": head.headers.get("Last-Modified", ""),
                "list_date": p["list_date"],
                "kind": parsed["kind"],
                "list_code": p["list_code"],
                "list_label": parsed["list_label"],
                "supplementary": parsed["supplementary"],
                "benches": parsed["benches"],
                "stats": parsed["stats"],
                "entries": parsed["entries"],
            }
            unknown = parsed["stats"]["unknown_types"]
            log.info("%s %s %-7s %4d entries%s", p["list_date"], p["list_code"], p["kind"], n,
                     f"  unknown types: {unknown}" if unknown else "")
            if out_dir:
                name = f"{p['list_date']}_{'advance_' if p['kind'] == 'advance' else ''}{p['list_code']}.json"
                (out_dir / name).write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            if client:
                result = client.send(payload)
                log.info("   app: %s", json.dumps(result)[:300])
                summary["sent"] += 1
            time.sleep(1)  # be gentle with sci.gov.in
        except Exception as exc:
            log.error("%s: %s", url, exc)
            summary["errors"].append(f"{url}: {exc}")

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    Path("status").mkdir(exist_ok=True)
    Path("status/last-run.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    log.info("Done: %s", json.dumps({k: v for k, v in summary.items() if k != "errors"}))
    return 1 if summary["errors"] else 0


def run_nclat(args) -> int:
    """NCLAT: read the daily-cause-list page, send each new or changed list."""
    from nclat.discover import discover as nclat_discover, new_session as nclat_session
    from nclat.parse import parse_pdf as nclat_parse

    session = nclat_session()
    client = None if args.dry_run else AppClient(args.app, args.token)
    pdfs, unrecognised = nclat_discover(session)
    if args.date:
        pdfs = [p for p in pdfs if p["list_date"] == args.date]
    if args.only:
        pdfs = [p for p in pdfs if p["list_code"] == args.only]
    log.info("NCLAT: found %d lists", len(pdfs))
    for name in unrecognised:
        log.warning("NCLAT: can't tell the court of %s (add it to nclat/discover.py)", name)

    known = {} if (client is None or args.force) else client.state("NCLAT")
    summary = {"started_at": datetime.now(timezone.utc).isoformat(), "court": "NCLAT",
               "found": len(pdfs), "sent": 0, "unchanged": 0, "unrecognised": unrecognised, "errors": []}
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    done = 0
    for p in pdfs:
        if args.max and done >= args.max:
            break
        url = p["url"]
        try:
            head = session.head(url, timeout=20, allow_redirects=True)
            if head.status_code != 200:
                continue
            # No ETag on nclat.nic.in; Last-Modified + size marks a change.
            etag = f'{head.headers.get("Last-Modified", "")}|{head.headers.get("Content-Length", "")}'
            if known.get(url) == etag:
                summary["unchanged"] += 1
                continue
            resp = session.get(url, timeout=120)
            resp.raise_for_status()
            parsed = nclat_parse(resp.content, p["court_no"], p["court_label"], p["bench"], p["supplementary"])
            done += 1
            n = len(parsed["entries"])
            if n == 0:
                raise RuntimeError("parsed 0 entries — parser needs attention")
            if parsed["list_date"] and parsed["list_date"] != p["list_date"]:
                log.warning("%s: header says %s, file name says %s (using file name)", url, parsed["list_date"], p["list_date"])
            payload = {
                "court": "NCLAT",
                "parser": NCLAT_PARSER_VERSION,
                "pdf_url": url,
                "etag": etag,
                "last_modified": head.headers.get("Last-Modified", ""),
                "list_date": p["list_date"],
                "kind": "daily",
                "list_code": p["list_code"],
                "list_label": parsed["list_label"],
                "supplementary": parsed["supplementary"],
                "bench": p["bench"],
                "benches": parsed["benches"],
                "stats": parsed["stats"],
                "entries": parsed["entries"],
            }
            unknown = parsed["stats"]["unknown_types"]
            log.info("%s %-10s %4d entries%s", p["list_date"], p["list_code"], n,
                     f"  unknown types: {unknown}" if unknown else "")
            if out_dir:
                (out_dir / f"nclat_{p['list_date']}_{p['list_code']}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            if client:
                result = client.send(payload)
                log.info("   app: %s", json.dumps(result)[:300])
                summary["sent"] += 1
            time.sleep(1)  # be gentle with nclat.nic.in
        except Exception as exc:
            log.error("%s: %s", url, exc)
            summary["errors"].append(f"{url}: {exc}")

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    Path("status").mkdir(exist_ok=True)
    Path("status/last-run-nclat.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    log.info("Done: %s", json.dumps({k: v for k, v in summary.items() if k != "errors"}))
    return 1 if summary["errors"] else 0


def run_dhc(args) -> int:
    """Delhi High Court: read the cause-list table, send each new or changed list."""
    from dhc.discover import discover as dhc_discover, new_session as dhc_session
    from dhc.parse import parse_pdf as dhc_parse

    session = dhc_session()
    client = None if args.dry_run else AppClient(args.app, args.token)
    pdfs, skipped = dhc_discover(session)
    if args.date:
        pdfs = [p for p in pdfs if p["list_date"] == args.date]
    if args.only:
        pdfs = [p for p in pdfs if p["list_code"] == args.only]
    log.info("DHC: found %d lists (%d other titles not read)", len(pdfs), len(skipped))

    known = {} if (client is None or args.force) else client.state("DHC")
    summary = {"started_at": datetime.now(timezone.utc).isoformat(), "court": "DHC",
               "found": len(pdfs), "sent": 0, "unchanged": 0, "not_read": skipped, "errors": []}
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    done = 0
    for p in pdfs:
        if args.max and done >= args.max:
            break
        url = p["url"]
        try:
            head = session.head(url, timeout=30, allow_redirects=True)
            if head.status_code != 200:
                log.warning("%s: HEAD %s", url, head.status_code)
                continue
            etag = head.headers.get("ETag", "") or f'{head.headers.get("Last-Modified", "")}|{head.headers.get("Content-Length", "")}'
            if known.get(url) == etag:
                summary["unchanged"] += 1
                continue
            resp = session.get(url, timeout=300)
            resp.raise_for_status()
            started = time.time()
            parsed = dhc_parse(resp.content, kind=p["kind"], supplementary=p["supplementary"], title=p["title"])
            done += 1
            n = len(parsed["entries"])
            if n == 0:
                # Supplementary "lists" are sometimes leave notes with no items.
                log.info("%s %-9s no items (%s)", p["list_date"], p["list_code"], p["title"][:60])
                continue
            if parsed["list_date"] and parsed["list_date"] != p["list_date"]:
                log.warning("%s: header says %s, table says %s (using table)", url, parsed["list_date"], p["list_date"])
            payload = {
                "court": "DHC",
                "parser": DHC_PARSER_VERSION,
                "pdf_url": url,
                "etag": etag,
                "last_modified": head.headers.get("Last-Modified", ""),
                "list_date": p["list_date"],
                "kind": p["kind"],
                "list_code": p["list_code"],
                "list_label": parsed["list_label"],
                "supplementary": parsed["supplementary"],
                "benches": parsed["benches"],
                "stats": parsed["stats"],
                "entries": parsed["entries"],
            }
            unknown = parsed["stats"]["unknown_types"]
            log.info("%s %-9s %5d entries in %.0fs%s", p["list_date"], p["list_code"], n, time.time() - started,
                     f"  unknown types: {dict(list(unknown.items())[:5])}" if unknown else "")
            if out_dir:
                (out_dir / f"dhc_{p['list_date']}_{p['list_code']}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            if client:
                result = client.send(payload)
                log.info("   app: %s", json.dumps(result)[:300])
                summary["sent"] += 1
            time.sleep(1)  # be gentle with delhihighcourt.nic.in
        except Exception as exc:
            log.error("%s: %s", url, exc)
            summary["errors"].append(f"{url}: {exc}")

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    Path("status").mkdir(exist_ok=True)
    Path("status/last-run-dhc.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    log.info("Done: %s", json.dumps({k: v for k, v in summary.items() if k not in ("errors", "not_read")}))
    return 1 if summary["errors"] else 0


def run_cghc(args) -> int:
    """Chhattisgarh High Court: find the day's lists, send each new or changed one."""
    from cghc.discover import discover as cg_discover, new_session as cg_session
    from cghc.parse import parse_pdf as cg_parse

    session = cg_session()
    client = None if args.dry_run else AppClient(args.app, args.token)
    pdfs = cg_discover(session)
    if args.date:
        pdfs = [p for p in pdfs if p["list_date"] == args.date]
    if args.only:
        pdfs = [p for p in pdfs if p["list_code"] == args.only]
    log.info("CGHC: found %d lists", len(pdfs))

    known = {} if (client is None or args.force) else client.state("CGHC")
    summary = {"started_at": datetime.now(timezone.utc).isoformat(), "court": "CGHC",
               "found": len(pdfs), "sent": 0, "unchanged": 0, "errors": []}
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)

    done = 0
    for p in pdfs:
        if args.max and done >= args.max:
            break
        url = p["url"]
        try:
            head = session.head(url, timeout=30, allow_redirects=True)
            if head.status_code != 200:
                continue
            etag = head.headers.get("ETag", "") or f'{head.headers.get("Last-Modified", "")}|{head.headers.get("Content-Length", "")}'
            if etag and known.get(url) == etag:
                summary["unchanged"] += 1
                continue
            resp = session.get(url, timeout=180)
            resp.raise_for_status()
            parsed = cg_parse(resp.content, kind=p["kind"], supplementary=p["supplementary"], registrar=p["registrar"])
            done += 1
            n = len(parsed["entries"])
            if n == 0:
                raise RuntimeError("parsed 0 entries — parser needs attention")
            if parsed["list_date"] and parsed["list_date"] != p["list_date"]:
                log.warning("%s: header says %s, the site says %s (using the site's)", url, parsed["list_date"], p["list_date"])
            payload = {
                "court": "CGHC",
                "parser": CGHC_PARSER_VERSION,
                "pdf_url": url,
                "etag": etag,
                "last_modified": head.headers.get("Last-Modified", ""),
                "list_date": p["list_date"],
                "kind": parsed["kind"],
                "list_code": p["list_code"],
                "list_label": parsed["list_label"],
                "supplementary": parsed["supplementary"],
                "benches": parsed["benches"],
                "stats": parsed["stats"],
                "entries": parsed["entries"],
            }
            unknown = parsed["stats"]["unknown_types"]
            log.info("%s %-7s %-7s %4d entries%s", p["list_date"], p["list_code"], p["kind"], n,
                     f"  unknown types: {unknown}" if unknown else "")
            if out_dir:
                (out_dir / f"cghc_{p['list_date']}_{p['list_code']}.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            if client:
                result = client.send(payload)
                log.info("   app: %s", json.dumps(result)[:300])
                summary["sent"] += 1
            time.sleep(2)  # be gentle with highcourt.cg.gov.in
        except Exception as exc:
            log.error("%s: %s", url, exc)
            summary["errors"].append(f"{url}: {exc}")

    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    Path("status").mkdir(exist_ok=True)
    Path("status/last-run-cghc.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    log.info("Done: %s", json.dumps({k: v for k, v in summary.items() if k != "errors"}))
    return 1 if summary["errors"] else 0


KHC_LISTS = [("B", "BLR", "https://judiciary.karnataka.gov.in/pdfs/consolidatedCauselist/blrconsolidation.pdf"),
             ("D", "DWD", "https://judiciary.karnataka.gov.in/pdfs/consolidatedCauselist/dwdconsolidation.pdf"),
             ("K", "KLB", "https://judiciary.karnataka.gov.in/pdfs/consolidatedCauselist/klbconsolidation.pdf")]


def run_khc(args) -> int:
    """Karnataka High Court: one consolidated PDF per bench, replaced each day with the next day's list."""
    import requests
    from khc.parse import parse_pdf as khc_parse

    session = requests.Session()
    session.headers["User-Agent"] = "OneCourtPro/1.0 (+https://onecourt.in; cause-list alerts)"
    client = None if args.dry_run else AppClient(args.app, args.token)
    known = {} if (client is None or args.force) else client.state("KHC")
    summary = {"started_at": datetime.now(timezone.utc).isoformat(), "court": "KHC",
               "found": len(KHC_LISTS), "sent": 0, "unchanged": 0, "errors": []}
    out_dir = Path(args.out) if args.out else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    for bench, code, url in KHC_LISTS:
        if args.only and args.only != code:
            continue
        try:
            head = session.head(url, timeout=30, allow_redirects=True)
            if head.status_code != 200:
                continue
            etag = head.headers.get("ETag", "") or f'{head.headers.get("Last-Modified", "")}|{head.headers.get("Content-Length", "")}'
            # The same address carries a new day's list each day, so the app knows each day's copy as "<url>#d=<date>".
            if etag and any(u.startswith(url) and e == etag for u, e in known.items()):
                summary["unchanged"] += 1
                continue
            resp = session.get(url, timeout=240)
            resp.raise_for_status()
            parsed = khc_parse(resp.content, bench)
            if not parsed["entries"] or not parsed["list_date"]:
                raise RuntimeError("parsed no entries or no date — parser needs attention")
            if args.date and parsed["list_date"] != args.date:
                continue
            payload = {
                "court": "KHC", "parser": KHC_PARSER_VERSION, "pdf_url": f"{url}#d={parsed['list_date']}", "etag": etag,
                "last_modified": head.headers.get("Last-Modified", ""), "list_date": parsed["list_date"], "kind": "daily",
                "list_code": code, "list_label": {"B": "Bengaluru", "D": "Dharwad", "K": "Kalaburagi"}[bench] + " bench list",
                "supplementary": False, "benches": [], "stats": parsed["stats"], "entries": parsed["entries"],
            }
            log.info("%s %s %4d entries", parsed["list_date"], code, len(parsed["entries"]))
            if out_dir:
                (out_dir / f"khc_{parsed['list_date']}_{code}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            if client:
                result = client.send(payload)
                log.info("   app: %s", json.dumps(result)[:300])
                summary["sent"] += 1
            time.sleep(2)
        except Exception as exc:
            log.error("%s: %s", url, exc)
            summary["errors"].append(f"{url}: {exc}")
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    Path("status").mkdir(exist_ok=True)
    Path("status/last-run-khc.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    log.info("Done: %s", json.dumps({k: v for k, v in summary.items() if k != "errors"}))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
