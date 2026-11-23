# Workday India jobs scraper

Finds jobs in **India** (country filter or an Indian city) on Workday career sites, keeps the ones that
match **keywords you choose**, and writes a filterable **HTML page** plus CSV and JSON.

Source of the sites: [kalil0321/ats-scrapers `workday.csv`](https://github.com/kalil0321/ats-scrapers/blob/main/ats-companies/workday.csv)
(about 5,300 Workday career sites). Python 3.10+, one dependency.

```bash
pip install httpx
```

## Quick start

```bash
cd workday-india-jobs
python3 workday_india_jobs.py                       # default keywords, last 14 days
```

A full run takes about 20 minutes. Progress is printed every 100 sites. When it finishes, open the
`.html` file it prints (in `output/<date>/`).

## Search with your own keywords

```bash
python3 workday_india_jobs.py -k "java,spring boot,microservices"
```

| Syntax | Meaning |
|---|---|
| `a,b,c` | comma separates keywords; a job matches if it contains **any** of them |
| `sre\|site reliability` | `\|` joins alternative spellings of one keyword; the first one is its label on the page |
| `"platform engineer"` | phrases are fine; each keyword must match as a **whole word** (`sre` does not match "Sreeram") |

Default keywords: `sre|site reliability,devops|dev ops|devsecops,kubernetes|k8s`.

## Options

| Option | Default | Effect |
|---|---|---|
| `-k, --keywords` | see above | keywords to search |
| `-m, --match` | `description` | `title`: keyword must be in the job title. `description`: title **or** job description. `any`: everything Workday's own search returns (noisy) |
| `-d, --days` | `14` | only jobs posted in the last N days (`0` = no limit) |
| `-o, --out` | `output` | output folder |
| `--company` | all | only sites whose name/tenant contains one of these words (comma separated), good for a quick test |
| `--max-sites N` | all | only the first N sites (testing) |
| `--csv` | `workday.csv` | sites list: a URL or local CSV with `name,url` columns. Default is the local `workday.csv` next to the script, downloaded on first use |
| `--refresh-csv` | off | download `workday.csv` again from GitHub before running |
| `--no-details` | off | skip the per-job detail request (faster; location may stay "2 Locations", no description) |
| `--workers`, `--concurrency` | 25, 25 | parallel sites / in-flight requests (lower them e.g. `10, 10` if you see many HTTP 429s) |
| `--rate` | 20 | max requests started per second across all sites (`0` = unlimited). Workday is behind Cloudflare, which rate-limits **per IP for all tenants**: too fast and most sites answer HTTP 429. `--rate` is the ceiling: on HTTP 429 the script pauses every request (10s, doubling up to 120s) **and halves its speed**, then speeds up again after clean requests, so it settles at what Workday currently tolerates (about 10 requests/s in practice) |
| `--cache`, `--no-cache` | `--no-cache` (off) | cache posting details and site India filters in `~/.cache/mncs-jobs-scraper/workday-details.sqlite`. Default is `--no-cache` (100% live probes). Enable with `--cache` to speed up repeat runs and prevent Cloudflare 429s |
| `--cache-days` | 14 | how old a cached posting detail entry may be when `--cache` is enabled |
| `--site-cache-days` | 7 | when `--cache` is enabled, a site's India filter (or "no India jobs") is cached too, so a repeat run skips its probe request (~4,400 of 5,300 sites cost 0 requests). Job keyword searches remain 100% live |
| `--retry-passes`, `--retry-wait` | 2, 60 | sites that failed with a temporary error (429, 5xx, timeout, or only some of their requests) are retried this many times, after this many seconds |
| `--timeout` | 30 | HTTP timeout in seconds |
| `--open` | off | open the HTML page in the browser when the run finishes |
| `--from-json FILE` | | rebuild the HTML from an earlier `.json` without scraping (can apply `-d`) |

Examples:

```bash
python3 workday_india_jobs.py -k "terraform,ansible" -m title -d 30      # titles only, last 30 days
python3 workday_india_jobs.py -k "golang" --company cisco,adobe          # quick test on a few companies
python3 workday_india_jobs.py --from-json output/<date>/workday_india_jobs_<stamp>.json -d 7   # last 7 days, no rescrape
```

## Run faster

The run is limited by Workday's per-IP rate limit, not by CPU or workers: in a measured run, 2,776 requests took 296s
(about 9 requests/s, which is where Workday starts to answer HTTP 429), and 68% of the requests were per-job detail
fetches (needed to check keywords in descriptions; Accenture alone has ~800 matching postings). Spreading requests
over several hosts does not help (tested: the limit is shared by all Workday hosts), so more `--workers` do not either.
What does help is making fewer requests:

- **Use `--cache` for fast repeat runs without HTTP 429s**:
  By default, caching is disabled (`--no-cache`), which probes all 5,300+ sites live on every run. Enabling `--cache` stores the site India filters (7 days) and job details (14 days) in SQLite (`~/.cache/mncs-jobs-scraper/workday-details.sqlite`). Job keyword search is still 100% live on all active sites, but skipping 4,400+ non-India site probes prevents Cloudflare IP rate limits and cuts run time drastically.
- `-m title` skips the per-job description request (title matches only), and `--no-details` skips the detail request for matches.
- Fewer keywords and a shorter `-d` window mean fewer requests.

```bash
# Recommended for routine scraping (fast, avoids 429s, live job search):
python3 workday_india_jobs.py -k "devops,sre,mlops,kubernetes,AI engineer" --cache --open

# Pure live scan without cache at gentler rate (prevents 429 blocks):
python3 workday_india_jobs.py -k "devops,sre,kubernetes" --rate 8 --workers 10 --concurrency 10 --open

# Fastest title-only search:
python3 workday_india_jobs.py -k "devops,sre" -m title --no-details --open
```

### Handling HTTP 429 Rate Limits
- **Why it happens**: Workday career sites are hosted behind Cloudflare. Sending thousands of probe requests across 5,300+ domains at high speed (`--rate 20`) can trigger Cloudflare's IP-level rate limiter.
- **Automatic backoff**: When a 429 occurs, the script automatically pauses all workers for 10s (escalating up to 120s if persistent) and dials down its request rate, retrying any temporarily failed sites at the end of the run (`--retry-passes 2`).
- **If you see repeated 429s**:
  1. Add `--cache` to avoid probing 4,400+ foreign sites over and over.
  2. Or lower concurrency and rate: `--rate 8 --workers 10 --concurrency 10`.
  3. If Cloudflare issues a hard IP challenge (HTML 429s), leave the IP idle for 30–60 minutes before retrying.
- Sites that still fail after all retry passes are listed in `workday_failed_sites_<stamp>.csv`; re-run only those failed sites later with:
  ```bash
  python3 workday_india_jobs.py -k "<keywords>" --csv output/<date>/workday_failed_sites_<stamp>.csv
  ```

## Output (`output/<date>/`)

Each run writes into a folder for the day (for example `output/2026-11-16/`), with files stamped with the run time, so earlier results are kept.

| File | Content |
|---|---|
| `workday_india_jobs_<stamp>.html` | self-contained page with filters (open it in a browser, no server needed) |
| `workday_india_jobs_<stamp>.csv` | company, title, location, posted, keywords, req id, type, URL |
| `workday_india_jobs_<stamp>.json` | the same plus job description and a `site` field |
| `workday_failed_sites_<stamp>.csv` | sites that could not be reached, with the HTTP reason |

### Filters on the HTML page

- Text box: title, company, location, requisition id, keyword (several words = all must match)
- Company, City (Bangalore/Bengaluru, Gurgaon/Gurugram etc. are merged), Time type (full/part time)
- **At any time**: Today, Last 1 / 2 / 3 / 7 / 15 days (counted from the day you open the page)
- Keyword chips (click one or more: jobs matching any of them)
- "keyword in title only" checkbox
- Click a column header to sort; Reset clears everything; the title links to the Workday posting

## How it works

For every site the script sends one request with no search text and reads the site's **facets**
(Workday's own filters). It builds an India filter from them: the country value "India", else any
location value naming India, an Indian city (Bangalore, Bengaluru, Hyderabad, Pune, Gurgaon,
Gurugram, Noida, Chennai, Mumbai and more), an Indian state (Telangana, Karnataka ...) or an `IN-<city code>-...`
site code (for example `IN-BLR-BCPV`), else any value containing the word India (for example a
hiring entity "... India Private Limited"). Sites with no India jobs stop there. For the others each
keyword is searched with that filter, results are checked against the keywords and the 14-day window,
and the posting detail is fetched for the matches (full location list, exact date, description).

`IN` alone is **not** treated as India, because it is the code for Indiana ("IN - Indianapolis").

## Sites the CSV gets wrong or lacks: `discover_sites.py`

The source CSV lists one site per company and some entries are stale. A plain run therefore misses:

1. **Moved tenants**: the site now lives on another Workday data center (for example Comcast, Lilly
   and NXP moved to `wd115` / `wd116`) or the site name has different capitalisation.
2. **Extra career sites**: companies often run several sites and the CSV lists only one
   (found this way: LPL Financial, Nationwide, Postman, Otis ...).

A Workday tenant publishes its live sites in `https://{tenant}.{wdN}.myworkdayjobs.com/robots.txt`;
`discover_sites.py` reads that and keeps the sites that answer the jobs API.

```bash
# 1. moved / mis-spelled sites: use the failed-sites file of a run
python3 discover_sites.py output/<date>/workday_failed_sites_<stamp>.csv -o output/discovered_sites.csv
python3 workday_india_jobs.py -k "<same keywords>" --csv output/discovered_sites.csv

# 2. extra sites of every tenant in the CSV (about 10 minutes)
python3 discover_sites.py workday.csv --new-only -o output/extra_sites.csv
python3 workday_india_jobs.py -k "<same keywords>" --csv output/extra_sites.csv
```

Each of those runs writes its own result files. Discovery results (`discovered_sites.csv`,
`extra_sites.csv`) do not depend on the keywords, so they can be reused for any later search.

## Unit tests

Offline (HTTP is faked, no Workday requests), 48 tests:

```bash
cd ATS/workday-india-jobs && python3 -m unittest discover -s tests -v
```

They cover keyword / date / India-facet parsing, the sites CSV (load, dedupe, local download), request retries, the
HTTP 429 pause and rate adaptation, description vs title matching, the posting-detail cache, partial failures, retry
passes, the date-folder output and `--open`.

## Known limits

- **HTTP 429 on thousands of sites.** An earlier version got HTTP 429 on 3,878 of 5,317 sites, so those sites (for
  example Barclays) were never searched. A/B tests showed it was not only speed: with httpx's default headers (or with
  only one of `Content-Type` / `Accept-Encoding` set) about half of the requests got a 429 even at 3 requests/s, while
  the same requests with an explicit `Content-Type: application/json` and `Accept-Encoding: identity` (what Python's
  `urllib` sends) were not limited. The script now sends those headers, paces requests (`--rate`), pauses everything on a
  429 and slows down, and retries failed sites at the end. If every request still answers 429 with an HTML page, wait
  a while and keep `--rate` low.
- **Unreachable sites** (about 90 of 5,300): retired or blocked sites answer HTTP 400/401/403/404/410/422
  and cannot be read. A site that redirects to Workday's maintenance page (HTTP 303) is worth retrying
  later: `python3 workday_india_jobs.py --csv output/<date>/workday_failed_sites_<stamp>.csv`.
- Only companies on Workday's public job interface are covered; companies that left Workday or use a
  private domain (for example Dell) cannot be found.
- Workday reports at most 2,000 hits per query; with an India filter plus a keyword this limit is not reached in practice.
- The location of multi-location jobs can be long; a few India offices appear only by address
  (for example "DLF CYBERCITY") or code (for example "IND07").
- Dates: Workday gives the exact posting start date only in the job detail, so `--no-details` shows
  the listing's relative date ("Posted 3 Days Ago") converted to a date.
