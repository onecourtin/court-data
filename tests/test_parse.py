"""
Parser tests against real SCI lists saved in tests/fixtures (01-10-2026 daily
lists and the 12-10-2026 advance list). Expected values were checked by hand
against the PDFs.
"""

from pathlib import Path

import pytest

from sci.parse import list_label, parse_pdf

FIX = Path(__file__).parent / "fixtures"


def _parse(name, code, kind="daily"):
    return parse_pdf((FIX / name).read_bytes(), list_code=code, kind=kind)


@pytest.fixture(scope="module")
def misc_main():
    return _parse("2026-10-01_M_J_1.pdf", "M_J_1")


@pytest.fixture(scope="module")
def misc_supp():
    return _parse("2026-10-01_M_J_2.pdf", "M_J_2")


@pytest.fixture(scope="module")
def registrar():
    return _parse("2026-10-01_M_R_1.pdf", "M_R_1")


@pytest.fixture(scope="module")
def advance():
    return _parse("advance_2026-10-12_M_J.pdf", "M_J", kind="advance")


def find(result, court_no, item_no):
    return next(e for e in result["entries"] if e["court_no"] == court_no and e["item_no"] == item_no)


def test_misc_main_list_header(misc_main):
    assert misc_main["list_date"] == "2026-10-01"
    assert misc_main["kind"] == "daily"
    assert misc_main["supplementary"] is False
    assert misc_main["list_label"] == "Miscellaneous — main list"
    assert [b["court_no"] for b in misc_main["benches"]] == [str(n) for n in range(2, 18)]


def test_first_page_items_are_not_lost(misc_main):
    # The old parser skipped each court's first page, losing items 1-3.
    for court in [str(n) for n in range(2, 18)]:
        mains = sorted({e["item_main"] for e in misc_main["entries"] if e["court_no"] == court})
        assert mains[0] == 1, court
        assert mains == list(range(1, mains[-1] + 1)), f"gap in court {court}"


def test_split_case_number_and_aor_code(misc_main):
    e = find(misc_main, "2", "2")   # "SLP(Crl) No." / "16213/2026" on two lines
    assert e["keys"] == ["SLPCRL|16213|2026"]
    assert e["petitioner"] == "SANDEEP RAJENDRA SINGH @ DEEPAK SINGH"
    assert e["respondent"] == "THE STATE OF MAHARASHTRA"
    assert e["aor_codes"] == [2441]
    assert e["section"] == "BAIL MATTERS"


def test_connected_diary_matter(misc_main):
    e = find(misc_main, "2", "5.1")
    assert e["connected"] is True
    assert e["keys"] == ["DIARY|55614|2026"]
    assert e["respondent"] == "SACHCHIDANAND SHUKLA AND ANR."


def test_judges_only_from_headers(misc_main):
    court2 = next(b for b in misc_main["benches"] if b["court_no"] == "2")
    assert court2["judges"] == ["Justice Vikram Nath", "Justice Sandeep Mehta"]
    assert court2["time"] == "10:30 AM"
    e = find(misc_main, "2", "1")
    assert e["judges"] == ["Justice Vikram Nath", "Justice Sandeep Mehta"] and e["time"] == "10:30 AM"


def test_each_item_carries_its_own_bench(misc_supp):
    # Court 2 also sits as a three-judge bench (items 301+) that day.
    regular = find(misc_supp, "2", "54")
    special = find(misc_supp, "2", "301")
    assert regular["judges"] == ["Justice Vikram Nath", "Justice Sandeep Mehta"]
    assert special["judges"] == ["Justice Vikram Nath", "Justice Augustine George Masih", "Justice Sandeep Mehta"]


def test_every_entry_has_a_case_number(misc_main, misc_supp, registrar, advance):
    for r in (misc_main, misc_supp, registrar, advance):
        assert r["stats"]["unknown_types"] == {}
        assert all(e["keys"] for e in r["entries"])


def test_misc_main_size(misc_main):
    assert 900 <= len(misc_main["entries"]) <= 960


def test_supplementary_wrapped_item_numbers(misc_supp):
    assert misc_supp["supplementary"] is True
    items = [e["item_no"] for e in misc_supp["entries"] if e["court_no"] == "11"]
    assert "56.10" in items and "56.19" in items
    assert "0" not in items
    assert find(misc_supp, "11", "56.10")["keys"] == ["SLPC|27812|2025"]


def test_registrar_courts(registrar):
    assert [(b["court_no"], b["judges"]) for b in registrar["benches"]] == [
        ("R1", ["Apoorv Singh, Registrar"]),
        ("R2", ["Gracy L. Bawitlung, Registrar"]),
    ]


def test_only_listed_note(registrar):
    # "Only CA No. 8980/2026 is listed under this item"
    assert find(registrar, "R1", "3")["listed"] is False
    assert find(registrar, "R1", "3.1")["listed"] is True


def test_chamber_and_single_judge_numbering():
    chamber = _parse("2026-10-01_M_C_1.pdf", "M_C_1")
    single = _parse("2026-10-01_M_S_1.pdf", "M_S_1")
    assert min(e["item_main"] for e in chamber["entries"]) == 1701
    assert min(e["item_main"] for e in single["entries"]) == 1601
    assert chamber["benches"][0]["judges"] == ["Justice Sanjeev Sachdeva"]


def test_advance_list(advance):
    assert advance["kind"] == "advance"
    assert advance["list_date"] == "2026-10-12"
    assert advance["list_label"] == "Advance list (tentative)"
    assert len(advance["entries"]) >= 1500
    assert all(e["court_no"] is None for e in advance["entries"])
    e = next(e for e in advance["entries"] if e["item_no"] == "517.1")   # "517." / "1" wrap
    assert "SLPC|33782|2017" in e["keys"] and "SLPC|33837A|2017" in e["keys"]


@pytest.mark.parametrize("code, kind, label", [
    ("M_J_1", "daily", "Miscellaneous — main list"),
    ("M_J_2", "daily", "Miscellaneous — supplementary list"),
    ("M_R_1", "daily", "Registrar — main list"),
    ("M_C_2", "daily", "Chamber — supplementary list"),
    ("F_J_1", "daily", "Regular hearing — main list"),
    ("M_J", "advance", "Advance list (tentative)"),
])
def test_list_labels(code, kind, label):
    assert list_label(code, kind) == label


def test_two_digit_sno_wrap_connected_item():
    """'16.1' + '19' on the next line is connected item 16.119, not a new item 19 (6 Oct 2026, Court 15)."""
    from pathlib import Path
    from sci.parse import parse_pdf
    data = (Path(__file__).parent / "fixtures" / "2026-10-06_M_J_1_wrap.pdf").read_bytes()
    e = parse_pdf(data, list_code="M_J_1", kind="daily")["entries"]
    hit = next(x for x in e if "DIARY|55402|2025" in x["keys"])
    assert hit["item_no"] == "16.119" and hit["connected"]
    assert (hit["petitioner"], hit["respondent"]) == ("STATE OF ODISHA AND ORS.", "PURNA CHANDRA PANDA")
    assert all(x["keys"] for x in e if x["item_no"].startswith("16.1"))
