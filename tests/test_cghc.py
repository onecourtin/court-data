"""
Chhattisgarh High Court tests against real lists saved in tests/fixtures/cghc
(pages cut from the 5 Oct 2026 daily, supplementary and weekly lists, and the
whole Additional Registrar list). Expected values checked by hand against the PDFs.
"""

from pathlib import Path

import pytest

from cghc.discover import _LOC_RE
from cghc.normalize import parse_case_token, remark_refs
from cghc.parse import parse_pdf

FIX = Path(__file__).parent / "fixtures" / "cghc"


def parsed(name, **kw):
    return parse_pdf((FIX / name).read_bytes(), **kw)


# ── case numbers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("token, key", [
    ("WPS/4131/2026", "CGWPS|4131|2026"),
    ("FA(MAT)/376/2025", "CGFAMAT|376|2025"),
    ("WP227/1000/2026", "CGWP227|1000|2026"),
    ("MCRCA/1656/2026", "CGMCRCA|1656|2026"),
    ("CRMP/0090/2026", "CGCRMP|90|2026"),
])
def test_case_token(token, key):
    assert parse_case_token(token)[0]["key"] == key


def test_unknown_type_is_reported_not_guessed():
    assert parse_case_token("ZZQ/1/2026") == (None, "ZZQ")


def test_connected_cases_in_remarks():
    assert remark_refs("* [ ON ADMISSION ] [ WITH WA 661/2026 & WPS 4131/2026 ] []") == ["CGWA|661|2026", "CGWPS|4131|2026"]
    assert remark_refs("( WITH CRMP NO. 2153/2026 FOR RESTORATION )") == ["CGCRMP|2153|2026"]
    assert remark_refs("[ ON I.A. NO. 01 APPLICATION FOR STAY ]") == []


def test_pdf_name_from_page():
    page = '<input type="hidden" id="pdf_location" name="pdf_location" value="CG05102026-SUP1.PDF">'
    assert _LOC_RE.search(page).group(1) == "CG05102026-SUP1.PDF"


# ── daily list ────────────────────────────────────────────────────────────

def test_daily_first_items():
    r = parsed("daily_2026-10-05_p1-12.pdf")
    assert r["list_date"] == "2026-10-05" and r["kind"] == "daily"
    e = r["entries"]
    first = e[0]
    assert (first["court_no"], first["item_no"], first["case_no_raw"]) == ("CGCJ", "1", "CRMP/2398/2026")
    assert first["keys"] == ["CGCRMP|2398|2026", "CGCRMP|2153|2026"]     # + the case named "WITH" it
    assert first["petitioner"] == "AMIR SINGH ALIAS GOLU AND 2 OTHERS"
    assert first["respondent"] == "STATE OF C.G. AND 1 OTHER"
    assert first["petitioner_aor"] == "PUSHP KUMAR GUPTA"
    assert first["respondent_aor"] == "A.G."
    assert first["judges"] == ["The Chief Justice", "Justice Ravindra Kumar Agrawal"]
    assert first["time"] == "02:15 P.M."
    assert first["section"] == "List 1 · Fresh Matters"
    assert "ON ADMISSION" in first["note"]
    assert e[1]["petitioner_aor"] == "DR. ARPIT LALL, AYUSH LALL, HARISH LALL"


def test_tied_up_judges_are_not_the_bench_and_remarks_stay_out_of_advocates():
    for x in parsed("daily_2026-10-05_p1-12.pdf")["entries"]:
        assert "TIED" not in x["petitioner"] + x["respondent"]
        assert "ADMISSION" not in x["petitioner_aor"] + x["respondent_aor"]
        assert "HON'BLE" not in x["respondent"]


def test_same_courtroom_two_sittings():
    e = parsed("daily_2026-10-05_p1-12.pdf")["entries"]
    morning = next(x for x in e if x["case_no_raw"] == "TAXC/67/2026")
    assert (morning["court_no"], morning["item_no"]) == ("CGCJ", "1")
    assert morning["judges"] == ["The Chief Justice", "Justice Santosh Sharma"]
    assert morning["time"] == "10:30 A.M."


# ── supplementary list: stops at "List Of Defective Cases" ────────────────

def test_supplementary_stops_at_defective_cases():
    r = parsed("supp_2026-10-05_sample.pdf", supplementary=True)
    assert r["supplementary"] and r["list_label"] == "Supplementary list"
    assert r["stats"]["stopped_at_defects"]
    keys = [k for x in r["entries"] for k in x["keys"]]
    assert "CGFAMAT|376|2025" in keys
    assert not any(k.startswith("CGMCC|212") for k in keys)   # filing numbers of defective cases
    assert r["entries"][0]["judges"] == ["Justice Parth Prateem Sahu", "Justice Sushma Sawant"]
    assert r["entries"][0]["court_no"] == "CG5"


# ── weekly list (tentative) and the Additional Registrar ──────────────────

def test_weekly_list_is_tentative():
    r = parsed("weekly_2026-10-05_p1-3.pdf", kind="advance")
    assert r["kind"] == "advance" and r["list_label"].startswith("Weekly list (tentative)")
    first = r["entries"][0]
    assert first["case_no_raw"] == "WPC/3299/2018"
    assert first["judges"] == ["The Chief Justice", "Justice Santosh Sharma"]     # EXCEPTION judges left out
    assert "Final" in first["section"]


def test_additional_registrar_list():
    r = parsed("ar_2026-10-05.pdf", registrar=True)
    assert len(r["entries"]) == 18
    assert {x["court_no"] for x in r["entries"]} == {"CGAR"}
    assert r["entries"][0]["case_no_raw"] == "CRA/907/2014"
    assert r["entries"][0]["judges"] == ["Additional Registrar (Judicial)"]
