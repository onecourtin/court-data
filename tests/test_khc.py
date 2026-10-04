"""
Karnataka High Court tests against pages of the real 5 Oct 2026 consolidated
lists (Bengaluru, Dharwad) in tests/fixtures/khc. Checked by hand against the PDFs.
"""

from pathlib import Path

from khc.parse import parse_pdf, type_code

FIX = Path(__file__).parent / "fixtures" / "khc"


def blr():
    return parse_pdf((FIX / "blr_2026-10-05_sample.pdf").read_bytes(), "B")


def test_type_codes():
    assert [type_code("WP"), type_code("CRL.A"), type_code("MFA.CROB")] == ["KWP", "KCRLA", "KMFACROB"]


def test_registrar_court_item():
    r = blr()
    assert r["list_date"] == "2026-10-05"
    e = next(x for x in r["entries"] if x["case_no_raw"] == "CRP 859/2026")
    assert (e["court_no"], e["item_no"], e["keys"]) == ("KB2A", "1", ["KCRP|859|2026"])
    assert (e["petitioner"], e["respondent"]) == ("SMT. AMRITA BANERJEE", "SRI MUNIRAJU T AND OTHERS")
    assert e["petitioner_aor"] == "NIKHIL K"            # advocates are the bold names
    assert e["judges"] == ["Joint Registrar 1"] and e["time"] == "10:45AM"


def test_chief_justice_bench_and_connected_matter():
    e = blr()["entries"]
    main = next(x for x in e if x["case_no_raw"] == "WA 1938/2026")
    assert main["court_no"] == "KB1"
    assert main["judges"] == ["The Chief Justice", "Justice K.S. Hemalekha"]
    conn = next(x for x in e if x["case_no_raw"] == "CCC 796/2025")
    assert conn["item_no"] == "1.1" and conn["connected"]


def test_party_names_stop_before_counsel_notes():
    for x in blr()["entries"]:
        assert "RES:" not in x["petitioner"]
        assert not x["respondent"].endswith("FOR R1")


def test_dharwad_bench_courts():
    r = parse_pdf((FIX / "dwd_2026-10-05_sample.pdf").read_bytes(), "D")
    assert r["entries"][0]["court_no"] == "KD9"
    assert r["entries"][0]["keys"] == ["KWP|108826|2016"]
