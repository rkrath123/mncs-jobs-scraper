# India jobs report (DevOps / SRE / MLOps / Kubernetes / LLMOps / HPC / AI Engineer)

One script that builds a filterable HTML report from the Stapply hosted job dataset.

| File | What it does |
|---|---|
| `jobs_report.py` | Queries the hosted snapshot, writes the raw data into a dated folder, then writes the HTML report into the same folder |
| `output/<YYYY-MM-DD>/` | One folder per run date, holding everything for that run |

Each dated folder contains:

| File | Contents |
|---|---|
| `india_jobs_report.html` | The report. Open it in any browser; it needs no server |
| `india_raw.parquet` | The extracted jobs the report is built from |
| `snapshot.txt` | When Stapply generated the dataset the run used |

Old reports stay in their own folders, so you can compare days.

## 1. Setup (once)

Python 3.11+ is needed.

```bash
python3 -m pip install duckdb pandas pyarrow
```

## 2. Run

Run the commands **from the repository root**, because the script uses relative paths like `all-ats-scrape/output/`.

```bash
cd /Users/rathram/Library/CloudStorage/OneDrive-IBM/Documents/ats-scrapers

# Pull matching jobs into all-ats-scrape/output/<today>/ (about 15-20 minutes, needs internet), then build the HTML report
python3 all-ats-scrape/jobs_report.py

# Open it (or add --open to the command above to open it in your browser when it finishes)
open all-ats-scrape/output/$(date +%F)/india_jobs_report.html      # macOS
```

The script uses today's date by default. To use a different date, pass `YYYY-MM-DD` as the argument, for example `python3 all-ats-scrape/jobs_report.py 2026-11-19`.

The extract step prints a row count and the output path when it finishes. It is slow because DuckDB has to read the `description` column of the 3.5 GB `all.parquet` file over HTTP. Run it in the background if you like:

```bash
nohup python3 all-ats-scrape/jobs_report.py > all-ats-scrape/extract.log 2>&1 &
tail -f all-ats-scrape/extract.log
```

If you only change the report layout, skip the slow extract and rebuild from an existing folder:

```bash
python3 all-ats-scrape/jobs_report.py --build-only            # today's folder
python3 all-ats-scrape/jobs_report.py --build-only --open     # ...and open it in the browser
python3 all-ats-scrape/jobs_report.py 2026-11-19 --build-only # a specific date
```

### Different keywords

Pass `-k` (or `--keywords`) with a comma-separated list to search for those instead of the defaults:

```bash
python3 all-ats-scrape/jobs_report.py -k "terraform,golang,c++,container"
```

- Keywords are plain, case-insensitive text matched in the job title or description (a keyword of 4 characters or fewer must match a whole word, so `go` does not match `google`).
- The output goes to its own folder, `all-ats-scrape/output/<date>_<keywords>/`, so your default report is not overwritten.
- It runs the full extract again (about 15-20 minutes). To rebuild that folder's HTML later, pass the same list: `python3 all-ats-scrape/jobs_report.py -k "terraform,golang,c++,container" --build-only`.
- For regex patterns (for example `dev[- ]?ops`) or friendlier labels, edit `KW` in the script instead.

## 3. Using the report

- **Date buttons:** Today, 1 day, 2 days, 3 days, 7 days, 30 days, All.
  - "Today" is the same calendar day in IST. The other buttons look back that many 24-hour periods.
  - **Relative to:** "snapshot time" measures from when the dataset was generated. "my current time" measures from your clock.
- **Posted column:** the posting date plus how long ago it was (today, yesterday, N days ago), measured from the "Relative to" setting.
- **Include undated:** jobs with no posting date (mostly SuccessFactors and Manatal) are hidden unless this box is ticked.
- **Match:** title and description, title only, or description only.
- **Topic:** one of the keywords. **City:** jobs whose location mentions that city, with job counts (spelling variants like Bangalore/Bengaluru are merged, and a job listing several cities appears under each). "No city given" covers locations such as just "India" or "Karnataka, India". **ATS:** one source. **Company:** type part of a name, or pick one from the list (with job counts); picking a full name shows only that company. **Search:** text in the title, company or location.
- Click a column header to sort. **Reset** restores the defaults.
- Blue tags are keywords found in the job title. Amber tags are keywords found only in the description.

## 4. Customising

All of these are in `jobs_report.py`.

| To change | Edit |
|---|---|
| Default keywords | `KW` (keyword to regex) near the top. The report picks new keywords up automatically. For one-off keyword lists use `--keywords` instead |
| Cities counted as India | `cities` list in the extract section |
| Rows loaded per scroll | `CHUNK` in the report's JavaScript |

The snapshot time shown in the report is read from `snapshot.txt` in the dated folder, which the extract step writes from the Stapply manifest.

### Posting dates from the job's own page

Stapply has no posting date for some ATSs, or only for some of their jobs. For these ATSs the script reads the date from the job's own page: **Workday** (its JSON endpoint), **SuccessFactors**, **recruiterbox**, **pinpoint**, **personio**, **talentbrew**, **ycombinator** and **avature** (only the Avature sites that show a "Date" field, such as Lenovo and Xerox). It uses `curl` (Workday throttles Python's `urllib`). Results are saved in `all-ats-scrape/posted_dates.json`, shared by all dates and keyword runs. A job's posting date never changes, so the first run takes about 15-20 minutes and later runs only look up newly seen jobs. Use `--skip-page-dates` to skip the lookup.

ATSs that publish no posting date anywhere stay "n/a": **manatal**, **jobvite**, **jazzhr**, **google**, **tesla**, and Avature sites without a date field (BMC, EA, Harman and others). A few jobs on the ATSs above also stay "n/a" when their site blocks the lookup (for example HTTP 403) or the job was removed. SuccessFactors sites that show the date as `1/16/26` are read as month/day/year.

## 5. Troubleshooting

- **`ModuleNotFoundError: duckdb`** — run the `pip install` line above.
- **`No module named 'pytz'`** — run `python3 -m pip install pytz`. DuckDB needs it when it converts timestamp columns in Python.
- **Download or timeout errors** — re-run `jobs_report.py`. It reads from `storage.stapply.ai`, so it needs a working connection.
- **`FileNotFoundError` for `all-ats-scrape/output/<date>/...`** — you used `--build-only` for a date with no extract yet. Run without `--build-only` first (or pass the date of an existing folder), and run from the repository root.
- **Empty "Today" results** — the data is a snapshot, so recent days can be empty. Use a wider range, or set "Relative to" to "snapshot time".

## Limits

- The data is the Stapply snapshot, usually about a day old. It is not a live scrape.
- Description matches are noisy. A job that mentions Kubernetes is not necessarily a DevOps role.
- Matching uses plain text rules (word boundaries for short terms like `sre` and `hpc`). Different spellings may be missed.
- India is detected from `country_iso`, the word "india" in the location (not "Little India, Singapore"), a listed city at the start of a word, or the bare codes some ATSs use instead of a place (`IN`, `IND-BLR-...`, `IN - HYD-...`, `Remote, in`, `Kochi, IN, 682303`). Remote jobs with no India location are not included.
- The dataset date for Breezy is often months too late, so Breezy jobs always use the date on their own page.
- HCLTech (about 2,200 jobs) and other sites whose pages show no date stay "n/a".
