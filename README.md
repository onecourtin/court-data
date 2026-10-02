# court-data — Supreme Court cause lists for OneCourt Pro

Reads the Supreme Court of India's published cause-list PDFs, turns every
entry into structured data, and sends it to the OneCourt app
(`app.onecourt.in`), which matches entries against users' matters and sends
listing alerts.

Runs free on GitHub Actions. It holds **no user data** — only public court
lists — so this repository can be public.

## What it reads

SCI's file server publishes each list as a PDF at a predictable address:

| URL | List |
|---|---|
| `api.sci.gov.in/jonew/cl/<date>/M_J_1.pdf` | Miscellaneous — main |
| `…/M_J_2.pdf` | Miscellaneous — supplementary |
| `…/M_C_1.pdf`, `M_C_2` | Chamber |
| `…/M_S_1.pdf`, `M_S_2` | Single judge |
| `…/M_R_1.pdf`, `M_R_2` | Registrar courts |
| `…/F_J_1.pdf`, `F_J_2` | Regular hearing |
| `api.sci.gov.in/jonew/cl/advance/<date>/M_J.pdf` | Advance list (≈2 weeks ahead, tentative) |

Timing seen on 1 Oct 2026: main list ~7 PM IST the evening before,
supplementary ~10 PM, registrar/chamber lists ~3 days ahead, advance lists
~2 weeks ahead.

Each PDF is fetched only when its ETag changes, with a 1-second pause between
downloads and an honest bot User-Agent. Case status and orders on
sci.gov.in sit behind a CAPTCHA and are **not** touched here; the app fetches
those only when a person solves the CAPTCHA.

## What it produces

One JSON payload per PDF: list date, kind (daily/advance), label, the bench
for each court, and one entry per item:

```json
{"court_no": "2", "item_no": "2", "case_no_raw": "SLP(Crl) No. 16213/2026",
 "keys": ["SLPCRL|16213|2026"], "primary_key": "SLPCRL|16213|2026",
 "petitioner": "SANDEEP RAJENDRA SINGH @ DEEPAK SINGH", "respondent": "THE STATE OF MAHARASHTRA",
 "petitioner_aor": "ANOOP PRAKASH AWASTHI- 2441", "aor_codes": [2441],
 "judges": ["Justice Vikram Nath", "Justice Sandeep Mehta"], "time": "10:30 AM",
 "section": "BAIL MATTERS", "connected": false, "listed": true}
```

**Keys** (`TYPE|number|year`) are how matters are matched — the app builds the
same key from the case type, number and year a user enters. Every case number
in an item is keyed, so "MA 1728/2022 in SLP(Crl) No. 1399/2020" alerts
whoever tracks either case. `listed: false` marks cases that an "Only … is
listed under this item" note excludes.

## Files

| File | Job |
|---|---|
| `sci/discover.py` | which lists exist right now |
| `sci/parse.py` | column-aware PDF parser |
| `common/normalize.py` | case number → key; AoR codes |
| `common/case_types_sci.json` | SCI case types and the spellings seen in lists (copy kept in the app) |
| `ingest_client.py` | sends payloads to the app |
| `run.py` | the command line |
| `.github/workflows/sci.yml` | the schedule |

## Running it

```bash
pip3 install -r requirements.txt
python3 -m pytest                                    # parser tests on saved lists
python3 run.py --dry-run                              # parse everything published, send nothing
python3 run.py --dry-run --date 2026-10-05 --out out/ # one date, keep the JSON
python3 run.py --app http://localhost:8780 --token <dev token>   # send to a local app
```

## Setting up on GitHub (once)

1. Push this folder to a **public** repo (free Actions minutes).
2. Settings → Secrets and variables → Actions → add
   `APP_INGEST_URL` (`https://app.onecourt.in`) and `APP_INGEST_TOKEN`
   (the same long random value as `INGEST_TOKEN` in the app's config).
3. Settings → Actions → General → Workflow permissions → *Read and write*.
4. Actions → "SCI cause lists → OneCourt app" → Run workflow, once by hand.

## When something breaks

A run fails (red in the Actions tab, plus an email from GitHub) if a list
parses to zero entries — it never sends an empty list, so good data in the app
is never wiped. Case types the parser doesn't recognise are reported in the
run log and on the app's admin page; add the new spelling to
`common/case_types_sci.json` (and the app's copy) and add a test.
