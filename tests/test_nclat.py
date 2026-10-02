"""
NCLAT tests against real lists saved in tests/fixtures/nclat (published
1–6 Oct 2026, page saved 2 Oct 2026). Expected values were checked by hand
against the PDFs.
"""

from datetime import date
from pathlib import Path

import pytest

from nclat.discover import classify, discover
from nclat.normalize import parse_case_numbers
from nclat.parse import parse_pdf

FIX = Path(__file__).parent / "fixtures" / "nclat"


def keys(text, chennai=False):
    return [r["key"] for r in parse_case_numbers(text, chennai_list=chennai)[0]]


# ── case numbers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text, chennai, expected", [
    ("Comp. App. (AT) (Ins) No. 1785 of 2025", False, ["NCAI|1785|2025"]),
    ("Comp. App. (AT) No. 351 of 2026", False, ["NCA|351|2026"]),
    ("Comp. App. (AT) (Ins) No. 1284 & 1285 of 2026", False, ["NCAI|1284|2026", "NCAI|1285|2026"]),
    ("Comp. App. (AT) (Ins) No. 1883 of 2025 & I.A. No. 7331, 7332 of 2025", False,
     ["NCAI|1883|2025", "NIA|7331|2025", "NIA|7332|2025"]),
    ("I.A. No. 6995 of 2026 in Comp. App. (AT) (Ins) No. 1125 of 2026", False, ["NIA|6995|2026", "NCAI|1125|2026"]),
    ("Restoration Application (AT) No. 41 of 2026 in Comp. App. (AT) No. 100 of 2024", False,
     ["NRESA|41|2026", "NCA|100|2024"]),
    ("Comp. App. (AT) No. 149 – 152 of 2026", False,
     ["NCA|149|2026", "NCA|150|2026", "NCA|151|2026", "NCA|152|2026"]),
    ("Competition App. (AT) No. 01 of 2026 & I.A. No. 2884, 5790 of 2026", False,
     ["NCOMPA|1|2026", "NIA|2884|2026", "NIA|5790|2026"]),
    ("I.A. No. 6957 of 2026 in Comp. App. (AT) No. ___ of 2026", False, ["NIA|6957|2026"]),
    # Chennai Bench: its own number series, so its own codes
    ("Comp App (AT) (CH) (Ins) No. 510/2026 For Stay IA No. 1436/2026", True, ["NCAICH|510|2026", "NIACH|1436|2026"]),
    ("IA No. 1473/2026 (For Stay) in Comp App (AT) (CH) (Ins) No. 518/2026", True, ["NIACH|1473|2026", "NCAICH|518|2026"]),
    ("Comp App (AT) (CH) (Ins) No.433/2023", False, ["NCAICH|433|2023"]),
])
def test_case_numbers(text, chennai, expected):
    assert keys(text, chennai) == expected


def test_primary_is_first_case():
    refs, _ = parse_case_numbers("I.A. No. 6995 of 2026 in Comp. App. (AT) (Ins) No. 1125 of 2026")
    assert [r["role"] for r in refs] == ["primary", "related"]


# ── file names ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name, code, court_no, supp, version", [
    ("Causelist_ch_05.10.2026.pdf", "COURT1", "N1", False, -1),
    ("Causelist_II_05.10.2026.pdf", "COURT2", "N2", False, -1),
    ("Causelist_IV_06.10.2026_0.pdf", "COURT4", "N4", False, 0),
    ("Supp_Causelist_ch_01.10.2026.pdf", "COURT1_S", "N1", True, -1),
    ("Supp_Causelist_IV_29.09.2026.pdf", "COURT4_S", "N4", True, -1),
    ("Registrar%20Court_01.10.2026.pdf", "REGISTRAR", "NR", False, -1),
    ("05.10.2026.pdf", "CHENNAI", "NCH", False, -1),
    ("06.10.2026_compressed.pdf", "CHENNAI", "NCH", False, -1),
    ("05.10.2026%20Suppl.pdf", "CHENNAI_S", "NCH", True, -1),
])
def test_classify(name, code, court_no, supp, version):
    c = classify(name)
    assert (c["list_code"], c["court_no"], c["supplementary"], c["version"]) == (code, court_no, supp, version)


def test_classify_rejects_other_pdfs():
    assert classify("nclat-STQC-Certificate-1.pdf") is None
    assert classify("Notice regarding holidays 05.10.2026.pdf") is None


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class _Session:
    def get(self, url, timeout=0):
        return _Resp((FIX / "daily-cause-list_2026-10-02.html").read_text(encoding="utf-8"))


def test_discover_from_saved_page():
    pdfs, unknown = discover(_Session(), today=date(2026, 10, 2))
    slots = {(p["list_date"], p["list_code"]) for p in pdfs}
    assert ("2026-10-05", "COURT1") in slots
    assert ("2026-10-05", "CHENNAI_S") in slots
    assert ("2026-10-06", "CHENNAI") in slots
    assert ("2026-10-07", "COURT4") in slots
    assert all(p["list_date"] >= "2026-10-02" for p in pdfs)
    assert len(slots) == len(pdfs)                  # one copy per court per day
    court4 = next(p for p in pdfs if (p["list_date"], p["list_code"]) == ("2026-10-06", "COURT4"))
    assert court4["url"].endswith("Causelist_IV_06.10.2026_0.pdf")
    assert court4["url"].startswith("https://nclat.nic.in/sites/default/files/")
    assert unknown == []


# ── whole lists ───────────────────────────────────────────────────────────

def _parse(name):
    c = classify(name)
    return parse_pdf((FIX / name).read_bytes(), c["court_no"], c["court_label"], c["bench"], c["supplementary"])


@pytest.mark.parametrize("name, count, list_date", [
    ("Causelist_ch_05.10.2026.pdf", 25, "2026-10-05"),
    ("Causelist_II_05.10.2026.pdf", 52, "2026-10-05"),
    ("05.10.2026.pdf", 60, "2026-10-05"),
    ("06.10.2026_compressed.pdf", 53, "2026-10-06"),
    ("Registrar Court_01.10.2026.pdf", 7, "2026-10-01"),
    ("Supp_Causelist_ch_01.10.2026.pdf", 3, "2026-10-01"),
])
def test_every_item_read_and_keyed(name, count, list_date):
    r = _parse(name)
    items = [e["item_main"] for e in r["entries"]]
    assert items == list(range(1, count + 1))
    assert all(e["keys"] for e in r["entries"])
    assert r["list_date"] == list_date
    assert r["stats"]["unknown_types"] == {}


def test_court_two_details():
    r = _parse("Causelist_II_05.10.2026.pdf")
    e = {x["item_no"]: x for x in r["entries"]}
    # item 4 sits alone at the top of page 2 (a one-row table)
    assert e["4"]["primary_key"] == "NCA|358|2026"
    assert e["4"]["petitioner"] == "Unitech Holdings Ltd."
    assert e["8"]["keys"] == ["NCA|149|2026", "NCA|150|2026", "NCA|151|2026", "NCA|152|2026"]
    assert e["1"]["section"] == "For Orders"
    assert e["1"]["respondent"] == "Amit Agarwal & Anr."
    assert r["benches"][0]["vc_link"] == "https://nclatvc.webex.com/meet/vc-court2"
    assert r["benches"][0]["judges"][0] == "Justice Sharad Kumar Sharma, Member (Judicial)"


def test_chairperson_court():
    r = _parse("Causelist_ch_05.10.2026.pdf")
    first = r["entries"][0]
    assert first["court_no"] == "N1"
    assert first["keys"] == ["NIA|6995|2026", "NCAI|1125|2026"]
    assert first["petitioner"].startswith("Joy Deb Chatterjee")
    assert first["respondent"] == "Kokila Mining Pvt. Ltd. & Anr."
    assert first["time"] == "10:30 AM"
    assert "Justice Yogesh Khanna, Officiating Chairperson" in r["benches"][0]["judges"]


def test_chennai_list_uses_chennai_codes():
    r = _parse("05.10.2026.pdf")
    assert all(k.split("|")[0].endswith("CH") for e in r["entries"] for k in e["keys"])
    assert r["entries"][0]["primary_key"] == "NCAICH|510|2026"
    # "A. With Interim Order" sub-headings must not hide items 23-30
    assert {e["item_main"] for e in r["entries"]} >= set(range(23, 31))
    assert "Note" not in " ".join(b["judges"][0] for b in r["benches"])
