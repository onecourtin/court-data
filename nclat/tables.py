"""
Table reader for NCLAT daily cause-list PDFs.

Copied from SLP/nclat/pdf_parser.py (the cause-list site's parser, proven on
live lists since July 2026) with the download step removed: it takes PDF
bytes. nclat/parse.py turns its items into OneCourt entries.

The Chairperson Court PDF (and others) exhibit two structural quirks:

1. SPLIT TABLES (page 1): The page is divided into a left table (S.No. + Case No.)
   and a right table (parties + counsel) with no S.No. column. These must be
   merged pair-wise to reconstruct full case records.

2. NO HEADER ROWS (pages 2+): Data rows begin immediately with S.No. — no header
   row precedes them. Column roles are detected from cell content instead.

HOW TO ADJUST IF NCLAT CHANGES THEIR FORMAT:
  1. Save the PDF into tests/fixtures and add a failing test.
  2. Update COLUMN_ALIASES for renamed headers.
  3. Update _detect_col_keys if column order changes.
"""

import io
import re
import logging
from typing import List, Dict, Optional
import pdfplumber

logger = logging.getLogger(__name__)

COLUMN_ALIASES: Dict[str, str] = {
    "s. no.": "sno", "s.no.": "sno", "sno": "sno",
    "sl. no.": "sno", "sl.no.": "sno", "s no": "sno",
    "item no.": "sno", "item no": "sno", "sr. no.": "sno",

    "case no.": "case_no", "case no": "case_no",
    "case number": "case_no", "comp. petition no.": "case_no",

    "name of the parties": "parties",
    "name of parties": "parties",
    "names of parties": "parties",
    "title of case": "parties",
    "parties": "parties",

    "counsel for appellants": "counsel_appellant",
    "counsel for appellant": "counsel_appellant",
    "counsel for appellant(s)": "counsel_appellant",
    "advocate for appellant": "counsel_appellant",
    "appellant's counsel": "counsel_appellant",

    "counsel for respondents": "counsel_respondent",
    "counsel for respondent": "counsel_respondent",
    "counsel for respondent(s)": "counsel_respondent",
    "advocate for respondent": "counsel_respondent",
    "respondent's counsel": "counsel_respondent",

    "remarks": "remarks",
    "coram": "coram",
    "bench": "coram",
}

_SN_RE = re.compile(r"^\d+\.?(?:\s+[A-Z]\.\s.*)?$")  # "42." or "42. B. Without Interim Order"
_CASE_NO_RE = re.compile(r"comp\.?\s*app|i\.a\.?\s*no|comp\.?\s*pet|appeal\s*no|petition\s*no", re.I)
_TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b", re.I)
# Captures from "In the Court of" up to the first section heading / table header
_COURT_OF_RE = re.compile(
    r"In the Court of\b(.+?)(?=\s+(?:For\s+(?:Admission|Hearing|Judgment|Orders?|Mention|Directions?|Miscellaneous)|S\s*\.\s*No\b|CASE\s*NO\b|\d{2}\s*\.\s*\d{2}\s*\.\s*\d{4})|\Z)",
    re.I | re.DOTALL
)
# Section-type headers that appear between tables
_SECTION_RE = re.compile(
    r"\bFor\s+((?:Final\s+Hearing|Passing\s+Orders?|Pronouncement\s+of\s+Judgment|"
    r"Admission(?:\s*\([^)]*\))?|Judgment|Orders?|Hearing|Mention|Directions?|"
    r"Miscellaneous|Condonation\s+of\s+Delay))\b",
    re.I
)


def _clean(val) -> str:
    return re.sub(r"\s+", " ", str(val or "")).strip()

def _normalise_header(cell: str) -> str:
    return re.sub(r"\s+", " ", str(cell or "").strip().lower())

def _map_header(cell: str) -> str:
    h = _normalise_header(cell)
    if h in COLUMN_ALIASES:
        return COLUMN_ALIASES[h]
    # A sub-heading such as "A. With Interim Order" can merge into the header
    # row ("a. with interim order s.no"); recognise the column by its end.
    for pattern, key in _HEADER_TAILS:
        if re.search(pattern, h):
            return key
    return h


_HEADER_TAILS = [
    (r"\b(?:s|sl|sr)\.?\s*no\.?$", "sno"),
    (r"case\s*no\.?$", "case_no"),
    (r"parties$", "parties"),
    (r"counsel for (?:the )?appellants?(?:\(s\))?$", "counsel_appellant"),
    (r"counsel for (?:the )?respondents?(?:\(s\))?$", "counsel_respondent"),
]

def _is_sn(cell) -> bool:
    return bool(_SN_RE.match(_clean(cell)))


def _detect_col_keys(data_rows: List[List], num_cols: int) -> List[str]:
    """
    Auto-detect column roles from cell content when no header row is present.

    Pattern observed: sno | case_no | [empty middle cols] | parties | counsel_app | counsel_app | counsel_resp
    The case_no is not always col 1 — sometimes col 1 is empty and case_no is col 2.
    The last 3 columns are always counsel_respondent, counsel_appellant x2.
    """
    col_keys = [""] * num_cols

    # Sample non-empty values from first 5 rows per column
    samples: List[List[str]] = [[] for _ in range(num_cols)]
    for row in data_rows[:5]:
        for i in range(num_cols):
            cell = row[i] if i < len(row) else None
            val = _clean(cell)
            if val:
                samples[i].append(val)

    # Col 0: S.No.
    if samples[0] and _is_sn(samples[0][0]):
        col_keys[0] = "sno"

    # First col after 0 (up to col 3) with case-number-like content → case_no
    for i in range(1, min(num_cols, 4)):
        if samples[i] and _CASE_NO_RE.search(" ".join(samples[i])):
            col_keys[i] = "case_no"
            break

    # Last 2 cols → counsel (only if not already identified as something else)
    if num_cols >= 2 and not col_keys[num_cols - 1]:
        col_keys[num_cols - 1] = "counsel_respondent"
    if num_cols >= 3 and not col_keys[num_cols - 2]:
        col_keys[num_cols - 2] = "counsel_appellant"

    # ALL middle cols (between case_no and counsel) → parties
    # We label every slot; _build_records skips cells with empty values
    case_no_idx = next((i for i, k in enumerate(col_keys) if k == "case_no"), 1)
    counsel_start = num_cols - 2 if num_cols >= 3 else num_cols
    for i in range(case_no_idx + 1, counsel_start):
        if col_keys[i] == "":
            col_keys[i] = "parties"

    return col_keys


def _build_records(col_keys: List[str], merged_data: List[List[str]]) -> List[Dict]:
    """Build case dicts from merged data rows, deduplicating same-key columns."""
    items: List[Dict] = []
    num_cols = len(col_keys)

    for cells in merged_data:
        record: Dict[str, str] = {}
        for i, key in enumerate(col_keys):
            if not key or i >= len(cells):
                continue
            val = cells[i]
            if not val:
                continue
            if key in record:
                if val not in record[key]:
                    record[key] = (record[key] + " " + val).strip()
            else:
                record[key] = val

        if not record.get("case_no") and not record.get("parties"):
            continue
        items.append(record)

    return items


def _merge_continuation_rows(data_rows: List[List], num_cols: int) -> List[List[str]]:
    """Merge continuation rows (where col[0] is empty) into the preceding case row."""
    merged_data: List[List[str]] = []
    current: Optional[List[str]] = None

    for row in data_rows:
        padded = [_clean(row[i]) if i < len(row) else "" for i in range(num_cols)]
        if _is_sn(padded[0]):
            if current is not None:
                merged_data.append(current)
            current = padded
        elif current is not None:
            for i, val in enumerate(padded):
                if val and i < len(current):
                    current[i] = (current[i] + " " + val).strip() if current[i] else val

    if current is not None:
        merged_data.append(current)

    return merged_data


def _parse_table(raw_table: List[List], learned_keys: Optional[Dict] = None) -> List[Dict]:
    """
    Parse a pdfplumber table into case-item dicts.
    Handles split headers, continuation rows, duplicate columns, and missing headers.

    learned_keys: dict of {num_cols: col_keys} shared across tables in the same PDF.
    When a headed table is parsed, its col_keys are stored in learned_keys.
    When a headerless table is parsed, learned_keys are used if available.
    """
    if not raw_table:
        return []
    # A one-row table is a lone item at the top of a page ("4. Comp. App. …").
    if len(raw_table) < 2 and not (raw_table[0] and _is_sn(raw_table[0][0])):
        return []

    num_cols = max(len(r) for r in raw_table)

    # Separate header rows from data rows
    header_rows: List[List] = []
    data_rows: List[List] = []
    found_data = False

    for row in raw_table:
        if found_data:
            data_rows.append(row)
            continue
        if _is_sn(row[0] if row else ""):
            found_data = True
            data_rows.append(row)
        else:
            header_rows.append(row)

    if header_rows:
        # Merge split header rows into one combined header
        merged_hdr = [""] * num_cols
        for row in header_rows:
            for i, c in enumerate(row):
                if i >= num_cols:
                    break
                val = _clean(c)
                if val:
                    merged_hdr[i] = (merged_hdr[i] + " " + val).strip() if merged_hdr[i] else val
        col_keys = [_map_header(h) for h in merged_hdr]
        # Fill any unmapped middle cols (between case_no and counsel) as 'parties'
        # so that headers with None cells don't produce gaps in learned_keys
        _cn = next((i for i, k in enumerate(col_keys) if k == "case_no"), 1)
        _counsel = [i for i, k in enumerate(col_keys) if k in ("counsel_appellant", "counsel_respondent")]
        _cs = min(_counsel) if _counsel else num_cols
        for _i in range(_cn + 1, _cs):
            if col_keys[_i] == "":
                col_keys[_i] = "parties"
        # Cache for subsequent headerless tables with the same column count
        if learned_keys is not None and num_cols not in learned_keys:
            learned_keys[num_cols] = col_keys
    elif data_rows:
        # No header row — use learned mapping if available, else detect from content
        if learned_keys and num_cols in learned_keys:
            col_keys = learned_keys[num_cols]
        else:
            col_keys = _detect_col_keys(data_rows, num_cols)
    else:
        return []

    merged_data = _merge_continuation_rows(data_rows, num_cols)
    return _build_records(col_keys, merged_data)


# ---------------------------------------------------------------------------
# Split-table handling (page 1 of Chairperson Court PDF)
# ---------------------------------------------------------------------------

def _is_left_only_table(table: List[List]) -> bool:
    """True if table has ≤2 non-empty columns and contains S.No. — left side of a split page.

    pdfplumber sometimes detects a spurious 3rd empty column from the page gutter;
    we count only columns that contain at least one non-blank cell.
    """
    if not table:
        return False
    ncols = max(len(r) for r in table)
    # Count columns that actually have content
    nonempty = sum(
        1 for col in range(ncols)
        if any(_clean(r[col] if col < len(r) else None) for r in table)
    )
    if nonempty > 2:
        return False
    return any(_is_sn(_clean(r[0])) for r in table if r and r[0] is not None)


def _is_right_only_table(table: List[List]) -> bool:
    """True if table has parties/counsel headers OR has no S.No. anywhere (headerless right table)."""
    if not table:
        return False
    for row in table[:3]:
        row_text = " ".join(str(c or "") for c in row).lower()
        if "parties" in row_text or "counsel" in row_text or "appellant" in row_text:
            return True
    # Headerless right table: ≥3 cols and col[0] is never a S.No.
    ncols = max(len(r) for r in table)
    if ncols >= 3 and not any(_is_sn(_clean(r[0])) for r in table if r):
        return True
    return False


def _parse_right_table_entries(table: List[List]) -> List[Dict]:
    """
    Parse a right-side table (parties + counsel, no S.No.) into case dicts.
    Case boundaries: col[0] is exactly '' (empty string, not None).
    """
    if not table:
        return []

    num_cols = max(len(r) for r in table)

    # Detect header rows by keyword
    merged_hdr = [""] * num_cols
    data_start = 0
    for i, row in enumerate(table):
        row_text = " ".join(str(c or "") for c in row).lower()
        if "parties" in row_text or "counsel" in row_text or "appellant" in row_text:
            for j, c in enumerate(row):
                if j >= num_cols:
                    break
                val = _clean(c)
                if val:
                    merged_hdr[j] = (merged_hdr[j] + " " + val).strip() if merged_hdr[j] else val
            data_start = i + 1
        else:
            break

    if any(merged_hdr):
        col_keys = [_map_header(h) for h in merged_hdr]
    else:
        # Positional fallback: col0 is always the empty case-boundary marker;
        # actual content (parties, counsel) starts at col1.
        if num_cols <= 2:
            col_keys = [""] + ["parties"] * (num_cols - 1)
        elif num_cols == 3:
            col_keys = ["", "parties", "counsel_respondent"]
        else:
            col_keys = ["", "parties"] + ["counsel_appellant"] * (num_cols - 3) + ["counsel_respondent"]

    # Split into cases: new case when col[0] is '' (empty string)
    entries: List[List[str]] = []
    current: Optional[List[str]] = None

    for row in table[data_start:]:
        padded = [_clean(row[i]) if i < len(row) else "" for i in range(num_cols)]
        col0_raw = row[0] if row else None

        if col0_raw == "" and any(v for v in padded[1:]):
            if current is not None:
                entries.append(current)
            current = padded
        elif current is not None:
            for i, val in enumerate(padded):
                if val and i > 0 and i < len(current):
                    current[i] = (current[i] + " " + val).strip() if current[i] else val

    if current is not None:
        entries.append(current)

    result = []
    for cells in entries:
        record: Dict[str, str] = {}
        for i, key in enumerate(col_keys):
            if not key or i >= len(cells):
                continue
            val = cells[i]
            if not val:
                continue
            if key in record:
                if val not in record[key]:
                    record[key] = (record[key] + " " + val).strip()
            else:
                record[key] = val
        if record.get("parties"):
            result.append(record)

    return result


def _merge_split_pair(left: List[List], right: List[List]) -> List[Dict]:
    """Zip left (sno+case_no) items with right (parties+counsel) entries."""
    left_items = _parse_table(left)
    right_entries = _parse_right_table_entries(right)

    result = []
    for i, item in enumerate(left_items):
        merged = dict(item)
        if i < len(right_entries):
            for key, val in right_entries[i].items():
                if val:
                    if key in merged and val not in merged[key]:
                        merged[key] = (merged[key] + " " + val).strip()
                    elif key not in merged:
                        merged[key] = val
        result.append(merged)

    return result


# ---------------------------------------------------------------------------
# Per-table context tracking (bench + section)
# ---------------------------------------------------------------------------

def _update_context(text: str, bench: dict, section: str):
    """
    Parse text found above a table to update the current bench/section context.
    Returns (new_bench_dict, new_section_str).
    """
    new_bench = dict(bench)
    new_section = section

    m = _TIME_RE.search(text)
    if m:
        t = re.sub(r"\s+", "", m.group(1)).upper()
        new_bench["time"] = re.sub(r"(AM|PM)$", r" \1", t)

    m = _COURT_OF_RE.search(text)
    if m:
        raw = re.sub(r"\s+", " ", m.group(1)).strip()
        raw = re.sub(r"\s+F\s*or\s+\w.*$", "", raw, flags=re.I | re.DOTALL).strip()
        new_bench["composition"] = "In the Court of " + raw

    m = _SECTION_RE.search(text)
    if m:
        new_section = "For " + re.sub(r"\s+", " ", m.group(1)).strip()

    return new_bench, new_section


# ---------------------------------------------------------------------------
# Bench composition extraction (time + judge names from page header text)
# ---------------------------------------------------------------------------

def _extract_bench_info(pdf) -> dict:
    """Extract sitting time and bench composition from the first 1–2 pages."""
    bench: dict = {}
    try:
        for page in pdf.pages[:2]:
            text = page.extract_text() or ""
            if not bench.get("time"):
                m = _TIME_RE.search(text)
                if m:
                    t = re.sub(r"\s+", "", m.group(1)).upper()
                    bench["time"] = re.sub(r"(AM|PM)$", r" \1", t)
            if not bench.get("composition"):
                m = _COURT_OF_RE.search(text)
                if m:
                    raw = re.sub(r"\s+", " ", m.group(1)).strip()
                    # Strip stray section headers appended by pdfplumber text merging
                    raw = re.sub(r"\s+F\s*or\s+\w.*$", "", raw, flags=re.I | re.DOTALL).strip()
                    bench["composition"] = "In the Court of " + raw
            if bench.get("time") and bench.get("composition"):
                break
    except Exception:
        pass
    return bench


# ---------------------------------------------------------------------------
# PDF fetching + multi-page extraction
# ---------------------------------------------------------------------------

def parse_tables(data: bytes) -> dict:
    """
    Read every table in an NCLAT cause-list PDF and return
      {'items': [...], 'bench_info': {'time': '...', 'composition': '...'}}

    Each item carries '_bench', '_time' and '_section' for the bench and
    cause-list section (e.g. "For Admission") it was listed under.
    """
    all_items: List[Dict] = []
    bench_info: dict = {}
    learned_keys: Dict[int, List[str]] = {}

    # Context persists across tables and pages
    current_bench: dict = {}
    current_section: str = ""

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        bench_info = _extract_bench_info(pdf)

        for page_num, page in enumerate(pdf.pages, start=1):
            try:
                table_objs = page.find_tables()
                tables_data = [t.extract() for t in table_objs]

                # Filter single-column sidebar artifacts
                filtered: List[tuple] = [
                    (obj, data) for obj, data in zip(table_objs, tables_data)
                    if data and max(len(r) for r in data) > 1
                ]

                if not filtered:
                    # No tables on this page — scan full text for context updates
                    page_text = page.extract_text() or ""
                    current_bench, current_section = _update_context(
                        page_text, current_bench, current_section
                    )
                    continue

                prev_bottom: float = 0.0
                i = 0

                while i < len(filtered):
                    obj, table = filtered[i]

                    # Scan text above this table to detect bench/section changes
                    try:
                        above = page.crop((0, prev_bottom, page.width, obj.bbox[1]))
                        above_text = above.extract_text() or ""
                    except Exception:
                        above_text = ""

                    current_bench, current_section = _update_context(
                        above_text, current_bench, current_section
                    )

                    # Detect split-table pair (left + right side-by-side)
                    if (_is_left_only_table(table)
                            and i + 1 < len(filtered)
                            and _is_right_only_table(filtered[i + 1][1])):
                        next_obj, next_table = filtered[i + 1]
                        items = _merge_split_pair(table, next_table)
                        prev_bottom = max(obj.bbox[3], next_obj.bbox[3])
                        i += 2
                    else:
                        items = _parse_table(table, learned_keys=learned_keys)
                        prev_bottom = obj.bbox[3]
                        i += 1

                    # Stamp each item with the current bench and section
                    for item in items:
                        if current_bench.get("time"):
                            item["_time"] = current_bench["time"]
                        if current_bench.get("composition"):
                            item["_bench"] = current_bench["composition"]
                        if current_section:
                            item["_section"] = current_section

                    all_items.extend(items)

            except Exception as exc:
                logger.warning("Page %d parse error: %s", page_num, exc)

    # De-duplicate by (case_no, parties)
    seen: set = set()
    deduped: List[Dict] = []
    for item in all_items:
        key = (item.get("case_no", ""), item.get("parties", ""))
        if key not in seen:
            seen.add(key)
            deduped.append(item)

    logger.info("Parsed %d items", len(deduped))
    return {"items": deduped, "bench_info": bench_info}
