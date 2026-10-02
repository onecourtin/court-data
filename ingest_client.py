"""
Talks to the OneCourt app's ingest API.

    GET  {APP}/api/ingest/state?court=SCI   -> {"pdfs": {"<pdf url>": "<etag>", ...}}
    POST {APP}/api/ingest                    <- one parsed cause list (JSON)

The token goes in both "Authorization: Bearer …" and "X-Ingest-Token: …":
some shared hosts strip the Authorization header before PHP sees it.
"""

from __future__ import annotations

import time
from typing import Dict

import requests


class AppClient:
    def __init__(self, base_url: str, token: str, session: requests.Session = None):
        if not base_url or not token:
            raise ValueError("APP_INGEST_URL and APP_INGEST_TOKEN must be set (or use --dry-run)")
        self.base = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.headers = {
            "Authorization": f"Bearer {token}",
            "X-Ingest-Token": token,
            "Accept": "application/json",
        }

    def _call(self, method: str, path: str, **kw) -> dict:
        last_exc = None
        for attempt in range(3):
            try:
                r = self.session.request(method, self.base + path, headers=self.headers, timeout=120, **kw)
                if r.status_code >= 500:
                    raise requests.HTTPError(f"{r.status_code}: {r.text[:300]}")
                if r.status_code >= 400:
                    # 4xx won't fix itself on retry (bad token, bad payload)
                    raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
                return r.json()
            except (requests.RequestException, ValueError) as exc:
                last_exc = exc
                time.sleep(3 * (attempt + 1))
        raise RuntimeError(f"{method} {path} failed after 3 attempts: {last_exc}")

    def state(self, court: str = "SCI") -> Dict[str, str]:
        pdfs = self._call("GET", "/api/ingest/state", params={"court": court}).get("pdfs") or {}
        return pdfs if isinstance(pdfs, dict) else {}

    def send(self, payload: dict) -> dict:
        return self._call("POST", "/api/ingest", json=payload)
