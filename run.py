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
    ap.add_argument("--court", choices=["sci", "nclat"], default="sci")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.court == "nclat":
        return run_nclat(args)
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


if __name__ == "__main__":
    sys.exit(main())
