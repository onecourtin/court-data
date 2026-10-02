import pytest

from common.normalize import extract_aor_codes, make_key, norm_number, parse_case_numbers, type_code


def keys(text):
    return [r["key"] for r in parse_case_numbers(text)[0]]


@pytest.mark.parametrize("text, expected", [
    ("SLP(Crl) No. 16213/2026", ["SLPCRL|16213|2026"]),
    ("SLP(C) No. 14728/2025", ["SLPC|14728|2025"]),
    ("Diary No. 48235-2026", ["DIARY|48235|2026"]),
    ("C.A. No. 5024/2023", ["CA|5024|2023"]),
    ("Crl.A. No. 2685/2026", ["CRLA|2685|2026"]),
    ("W.P.(C) No. 1195/2026", ["WPC|1195|2026"]),
    ("W.P.(Crl.) No. 199/2026", ["WPCRL|199|2026"]),
    ("T.P.(Crl.) No. 1234/2026", ["TPCRL|1234|2026"]),
    ("ORGNL.SUIT No. 1/2025", ["OS|1|2025"]),
    ("ARBIT.PETITON No. 18/2026 PIL-W", ["ARBP|18|2026"]),       # SCI's own spelling
    ("SMW(C) No. 4/2021 PIL-W", ["SMWPC|4|2021"]),
    ("C.A. No. 20-21/2024", ["CA|20|2024", "CA|21|2024"]),
    ("C.A. No. 1234-36/2024", ["CA|1234|2024", "CA|1235|2024", "CA|1236|2024"]),
    ("S.L.P.(C)...CC No. 5031-5032/2014", ["SLPCCC|5031|2014", "SLPCCC|5032|2014"]),
    ("MA 1728/2022 in SLP(Crl) No. 1399/2020", ["MA|1728|2022", "SLPCRL|1399|2020"]),
    ("CONMT.PET.(C) No. 1986/2017 in W.P.(C) No. 732/2017 PIL-W", ["CONTC|1986|2017", "WPC|732|2017"]),
    ("MA 1390/2026 in MA 239/2024 in W.P.(Crl.) No. 242/2019", ["MA|1390|2026", "MA|239|2024", "WPCRL|242|2019"]),
    ("R.P.(Crl.) No. 24/2023 in SLP(Crl) No. 438/2022", ["RPCRL|24|2023", "SLPCRL|438|2022"]),
    ("Only CA No. 8980/2026 is listed under this item", ["CA|8980|2026"]),
    ("Ref.U/A 317(1) No. 1/2018", ["REF317|1|2018"]),
    ("C.A. D 39767/2014", ["DIARY|39767|2014"]),           # civil appeal known by diary no.
])
def test_keys(text, expected):
    assert keys(text) == expected


def test_primary_and_related_roles():
    refs, unknown = parse_case_numbers("MA 1728/2022 in SLP(Crl) No. 1399/2020")
    assert [r["role"] for r in refs] == ["primary", "related"]
    assert unknown == []


def test_range_with_letter_suffix():
    k = keys("SLP(C) No. 33782-33837A/2017")
    assert k[0] == "SLPC|33782|2017"
    assert "SLPC|33837|2017" in k and "SLPC|33837A|2017" in k
    assert len(k) == 57


def test_unknown_type_reported_not_keyed():
    refs, unknown = parse_case_numbers("XYZ.PET. No. 5/2026")
    assert refs == [] and unknown == ["XYZ.PET."]


def test_make_key_matches_what_the_app_builds():
    assert make_key("SLPC", "012345", 2025) == "SLPC|12345|2025"
    assert make_key("SLPC", "33837a", "2017") == "SLPC|33837A|2017"
    assert norm_number(" 7 ") == "7"
    assert type_code("S.L.P.(Crl.)") == "SLPCRL"


@pytest.mark.parametrize("text, expected", [
    ("RAJ KISHOR CHOUDHARY- 2212", [2212]),
    ("ARVIND KUMAR SHARMA- 1133 [R-1], [R-2], [R-3], ALJO K. JOSEPH- 2529 [R-4], P. S. SUDHEER- 1603 [R-5]", [1133, 1603, 2529]),
    ("LAKSHMI RAMAN SINGH- 314[R-1], Y. RAJA GOPALA RAO- 1210[R-1]", [314, 1210]),
    ("ADITYA JAIN-1- 2985 [CAVEAT]", [2985]),
    ("KARANJAWALA & CO.- 1707 , [CAVEAT], SANJAY KUMAR VISEN- 1738 ,", [1707, 1738]),
    ("", []),
])
def test_aor_codes(text, expected):
    assert extract_aor_codes(text) == expected
