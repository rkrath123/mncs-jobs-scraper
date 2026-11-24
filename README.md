# MNC job scrapers (you choose the keywords)

One scraper per sector. Each searches the career sites of its companies for the keywords you give
(for example `sre,devops,kubernetes,mlops` or `java,testing`) in India, and reports the jobs that appeared since
the last run. No keyword is built in.
Python 3 standard library only.

**Rule: product-based companies only.** No consultancies, IT-services, BPO, EPC / engineering-services,
inspection / certification, CRO or staffing firms, and no India-origin companies (they are not foreign MNCs).
The rule is recorded in `sectors/policy.json`; everything it leaves out is listed with the reason in
`sectors/excluded.json`, so any decision can be reversed.

## Structure

```
data/international_mnc_job_search_directory_india.txt   source list (35 sectors, ~1,000 companies)
sectors/policy.json              sector map, renames to the parent company, exclusion lists, product-only rule
sectors/excluded.json            generated: what was left out and why
sectors/<sector>/candidates.json generated: companies still to verify for that sector
sectors/<sector>/companies.json  verified companies (career site returns India jobs) = what gets scraped
output/<sector>/                 reports and seen_jobs.json
jobs_engine.py                   all platform fetchers, report, state (shared)
scrape_sector.py                 runs any sector:  python3 scrape_sector.py <sector>
tools/build_candidates.py        directory -> candidates per sector (applies policy.json)
tools/discover.py                guesses Workday / Greenhouse / Lever / SmartRecruiters ids, counts India jobs
tools/sniff.py                   shows which platform a career page uses
tools/verify.py                  runs a sector with broad keywords and prints India jobs per company
tools/prune.py                   removes companies from a sector's companies.json
```

`python3 scrape_sector.py --list` shows every sector with its verified companies and what is still to verify.

| Tier | Sector | Verified companies (scraped + manual) |
|---|---|---|
| done | `fintech` (`fintech_scrape_jobs.py`) | 96 + 3 |
| done | `energy` (`energy_scrape_jobs.py`) | 59 + 7 |
| 1 | `cybersecurity` | 18 |
| 1 | `software_cloud` | 25 + 1 (Microsoft, manual) |
| 1 | `semiconductors_hardware` | 19 |
| 1 | `telecom_networking` | 14 |
| 2 | `healthcare_pharma` | 50 |
| 2 | `industrial_manufacturing` | 24 |
| 2 | `automotive` | 15 |
| 2 | `aerospace_defence` | 8 |
| 2 | `ecommerce_retail` | 11 |
| 2 | `media_entertainment` | 8 |
| 2 | `travel_tech` | 6 |
| 2 | `data_information` | 4 |
| 3 | `fmcg_food` 14, `education_publishing` 8, `chemicals_materials` 14, `agriculture` 4, `fashion_lifestyle` 4, `logistics_transport` 13, `airlines_aviation` 2, `mining_metals` 3, `water_environment` 1 | operator / non-tech sectors, thin coverage |
| 2 | `gaming` | 4 |
| 3 | `hospitality` | 4 |
| 3 | `security_systems` | 1 |

Coverage per sector is limited by what can be scraped: many large companies (for example Walmart, Ford, Bosch,
Netflix, Meta, Google, Apple, Toyota, Nestle) use custom or protected career sites and are still in
`sectors/<sector>/candidates.json` for a manual check.

`fintech_scrape_jobs.py` and `energy_scrape_jobs.py` are kept so `colab.py` and cron lines keep working; they call
the same engine as `scrape_sector.py`.

**Quick start:** `python3 scrape_sector.py cybersecurity --keywords "sre,devops" --title-only --open` runs the scan and opens the browser
report. The report is also saved as `output/<sector>/latest.html`.

## All sectors in one report

`scrape_all.py` runs every sector that has a `companies.json` in one parallel pool and writes **one** HTML report
with a **Sector** dropdown (plus the usual company / city / keyword / age filters). Pick the sector in the browser,
or limit the run on the command line:

```bash
python3 scrape_all.py --keywords "sre,devops,kubernetes,mlops" --show-all --open   # all sectors, one report
python3 scrape_all.py --keywords "java,testing" --title-only --open                # only titles that contain a keyword
python3 scrape_all.py --keywords sre --sectors cybersecurity,fintech,software_cloud --show-all
python3 scrape_all.py --keywords sre --skip-sectors energy,fintech                 # everything except these
python3 scrape_all.py --help-engine                           # the other options (same as scrape_sector.py)
```

Sites that fail (HTTP 521/5xx, DNS errors) are retried once after 20 s (`--retry-failed N`, `--retry-wait S`). Any that
still fail are listed at the end of the run and in a yellow banner at the top of the HTML report, so a missing
company is never silent. An HTTP 521 on many Workday companies at once is a Workday-side outage: run again later.

Output: `output/all/latest.html`, `output/all/<date>/{report.html,jobs.csv,jobs.md}` and `output/all/seen_jobs.json`
(its own "new jobs" state; the per-sector reports in `output/<sector>/` are not touched). `jobs.csv` gets a `sector`
column.

## Run

`scrape_sector.py <sector>` and the two sector scripts take the same options (shown here for fintech):

```bash
python3 scrape_sector.py software_cloud --keywords "java,testing" --show-all   # any sector with a companies.json
python3 fintech_scrape_jobs.py --keywords "sre,devops"             # new jobs from the last 14 days, India
python3 fintech_scrape_jobs.py --keywords sre --title-only         # only titles that contain a keyword (less noise)
python3 fintech_scrape_jobs.py --keywords sre --show-all           # everything that matches, not only new jobs
python3 fintech_scrape_jobs.py --keywords sre --companies citi,visa,lseg
python3 fintech_scrape_jobs.py --keywords sre --country Singapore --country-code SG
python3 fintech_scrape_jobs.py --keywords "kubernetes,terraform,sre" --max-age-days 7
python3 fintech_scrape_jobs.py --keywords sre --skills "Terraform,Python,AWS"   # tags these skills on found jobs
python3 fintech_scrape_jobs.py --keywords sre --workers 16         # fewer parallel requests if a site rejects them
```

A full scan takes about 3 to 8 minutes depending on your network (fintech 92 sites, energy 57 sites).

Each run prints a table and writes `output/<sector>/<date>/report.html` (also copied to
`output/<sector>/latest.html`), `jobs.csv` and `jobs.md`.

The HTML report is a single file with clickable job links. It lists every matching job, marks the ones
first seen today as NEW, and has a search box, company / city / keyword / platform / match / age filters, a
"Posted today only" checkbox and sortable columns (click a header). It also lists the career sites the
scraper can't cover, as links.

`output/<sector>/seen_jobs.json` remembers every job already reported, so the next day only shows new ones.
Running twice on the same day gives the same report. Delete it to reset.

**Remote / WFH:** every job gets a work mode (`remote`, `hybrid` or empty = on-site / not stated), shown in the
Mode column, in `jobs.csv` (`work_mode`) and as a **Work mode** filter in the report. `--remote-only` keeps only remote
jobs. Remote is detected from the title / location ("Remote - India", "WFH") and from explicit description phrases
("work from home", "fully remote"); ordinary uses such as "remote servers" are ignored. Many career sites do not state
the work mode at all, so a job shown as on-site may still allow remote work.

The Match column ("title" or "description") shows whether the keyword is in the job title.
`relevance=description` means it only appears in the job description, which is often noise.

## Run faster (more workers)

`--workers N` is the number of career sites fetched in parallel (default 32). Raise it to finish sooner, lower it
if sites start answering HTTP 429 / timeouts. The same option works for `scrape_all.py`, `scrape_sector.py` and the
sector scripts; `-k` is short for `--keywords`.

```bash
python3 scrape_all.py -k "devops,sre,mlops,kubernetes,AI engineer" --show-all --workers 64 --open   # faster
python3 scrape_all.py -k "devops,sre" --workers 16                                                # gentler, fewer 429s
python3 scrape_all.py -k "devops,sre" --sectors fintech,software_cloud,cybersecurity --workers 64   # fewer sectors = fastest
python3 scrape_all.py -k "devops,sre" --retry-failed 2 --retry-wait 30                            # retry rate-limited sites
```

- The slowest stage is the description fetch (one request per Workday / SmartRecruiters job); `--workers` speeds it up too.
- Fewer keywords run faster: each keyword is one search per company.
- Very high worker counts make Workday, Eightfold and Oracle more likely to answer HTTP 429 (not measured here: try 64, then adjust). Failed sites
  are retried after `--retry-wait` seconds and listed at the end of the run.

## Keywords and skills

Nothing is hardcoded. Without `--keywords`, a terminal run asks for them; a non-interactive run (cron, Colab
subprocess) stops with an error.

- **Keywords** (`-k "a,b,c"` or `--keywords "a,b,c"`, comma separated; a slash is part of a term, so `CI/CD` and `AI/ML` work) are
  searched on every career site. A job is only found if one of them matches. Each keyword is one search per company,
  so fewer, specific keywords run faster.
- **Title match:** a job is a "title" match when a keyword appears in its title. `--title-words "qa,test*,sdet"`
  adds more title words (whole word, case-insensitive; a trailing `*` means "starts with"). `--title-only` keeps only
  title matches.
- **Skills** (`--skills "a,b,c"`, optional) are not searched. They are matched against the title and description of
  the jobs the keywords found, and show up in the Keywords column and the Keyword filter. A skill in the title makes
  the job a "title" match. A job that mentions only a skill and none of the keywords is not found: put the term into
  `--keywords` to search for it.
- Descriptions come from the career site's list API where it provides them, and from one extra request per job
  on Workday and SmartRecruiters sites. Goldman Sachs, HSBC, Moody's, Citizens and MSCI give no description, so
  only their titles are checked for skills.

## Run every day (cron)

```bash
crontab -e
```
```
0 9 * * * cd /Users/rathram/Library/CloudStorage/OneDrive-IBM/Documents/mncs-jobs-scraper && /usr/bin/python3 fintech_scrape_jobs.py --keywords "sre,devops" --title-only >> run.log 2>&1
5 9 * * * cd /Users/rathram/Library/CloudStorage/OneDrive-IBM/Documents/mncs-jobs-scraper && /usr/bin/python3 energy_scrape_jobs.py --keywords "sre,devops" --title-only >> run.log 2>&1
10 9 * * * cd /Users/rathram/Library/CloudStorage/OneDrive-IBM/Documents/mncs-jobs-scraper && for s in cybersecurity software_cloud semiconductors_hardware telecom_networking; do /usr/bin/python3 scrape_sector.py $s --keywords "sre,devops" --title-only; done >> run.log 2>&1
```

## Coverage by platform (fintech and energy; the tier-1 sectors use the same platforms)

Also supported: **Amazon** (`amazon.jobs` search.json, India = `country=IND`), **Atlassian** (one public JSON
listing), **Apple** (`jobs.apple.com/api/v1/search`, India = `postLocation-INDC`), **Avature** (Synopsys, Lenovo, Tesco, EA:
`SearchJobs/<keyword> India` HTML pages), **Cornerstone** (Linde, Henkel: `rec-job-search` API with the token from the
career page) and **Taleo** (Mercedes-Benz: `rest/jobboard/searchjobs` narrowed by the India location facet), **Google** (the results page embeds the job list as JSON), **Meta** (metacareers GraphQL; the persisted `doc_id` can be overridden in the company entry), **IBM** (`www-api.ibm.com/search/api/v2`) and **SAP** (jobs.sap.com embeds the list; no keyword search, so matched on title and team). Microsoft uses Eightfold `pcsx` with `"delay"`: it answers HTTP 429 every few pages, so a Microsoft scan takes several minutes. Tier-1 sectors use Workday (CrowdStrike, Palo Alto, F5, NVIDIA, Intel, Cisco, Salesforce, Adobe, ...),
Greenhouse (Okta, Zscaler, Rubrik, Pure Storage, ...), Jibe (AMD, Keysight, DocuSign, McAfee), Eightfold
(Qualcomm, Ericsson, Vodafone, Infineon, Applied Materials, Lam Research), Oracle HCM (Oracle, Texas
Instruments, Fortinet), SuccessFactors (NetApp, BT, CommScope), SmartRecruiters (ServiceNow, Arista, Check Point)
and Lever (Extreme Networks).

| Platform | Fintech | Energy | How |
|---|---|---|---|
| Workday | 58 sites (Wells Fargo, Citi, NatWest, Mastercard, Barclays, Capital One, ...) | 29 (Shell, BP, Chevron, Baker Hughes, KBR, ABB, GE Vernova, Caterpillar, ...) | `wday/cxs` JSON API |
| Oracle HCM | JPMorganChase, Chubb, BNY, American Express | Technip Energies, DNV, Intertek, Wood, Honeywell, Emerson, Cummins, NOV, Oceaneering, Vertiv, ABS, UL Solutions | `hcmRestApi` JSON API |
| SuccessFactors | ANZ, Standard Chartered, Swiss Re, Worldline | ExxonMobil, Halliburton, TechnipFMC, Bureau Veritas, TUV SUD, Danfoss, Alstom, Vestas, Wartsila | RSS search feed |
| Eightfold | HSBC | Eaton, Worley, Fluor | `/api/apply/v2/jobs`, or `/api/pcsx/search` with `"api": "pcsx"` |
| Jibe (iCIMS) | Aon, Gallagher, ICE, AXA | Schneider Electric | `/api/jobs` JSON API |
| Greenhouse | Coinbase, Tide, Stripe, Adyen, Payoneer, Clearwater Analytics, Block | Cognite | `boards-api.greenhouse.io` |
| SmartRecruiters | Experian, Avaloq, Wise | SGS | `api.smartrecruiters.com` postings API |
| Lever | Dun & Bradstreet, Nium | | `api.lever.co` postings API |
| Phenom | Allianz | | `/widgets` search API |
| Radancy (TalentBrew) | Moody's, Citizens, Intuit | | search-results HTML fragment, country matched client side |
| iCIMS portal | MSCI | | server-rendered search pages |
| BrassRing | UBS | | `PowerSearchJobs` with the session token from the home page |
| In-house | Goldman Sachs (GraphQL), D. E. Shaw (scans position pages), Bank of America (search servlet, newest 100 India jobs) | | Goldman, D. E. Shaw and Intuit give no posting date |
| Manual links | Revolut, BNP Paribas (block bots), Societe Generale (search service unreachable) | SLB, TotalEnergies and Jacobs (Avature, not supported yet), Siemens Energy, Siemens, Weatherford, AtkinsRealis | listed as links under `manual` in the company file and in the report |

Only companies whose career site returned India jobs were kept. Some of them had no keyword match in the last
14 days and show no jobs (fintech: D. E. Shaw, Citizens, Block, Nium, ...; energy: Air Products, Flowserve,
Prysmian, WEG). Companies tried and dropped because their career sites list no India jobs: Truist, PNC, BMO,
Circle, Travelers (fintech); Equinor, Canadian Solar, RWE, Eni, Subsea7, EDF Renewables, Element Materials
(energy). Energy companies with an India office but no scrapable career site found are not covered yet
(for example Linde, Air Liquide, Siemens Gamesa, Enphase and most solar manufacturers).

## Add a company or a sector

Add a company by copying an entry in the sector's company file.

- Other platforms take the keys used in the company file (for example `host`, or `hosts` for iCIMS).
- For Workday you need `host`, `tenant` and `site`. They appear in the career-site URL, for example
  `https://citi.wd5.myworkdayjobs.com/en-US/2/...` gives host `citi.wd5.myworkdayjobs.com`, tenant `citi`, site `2`.
  If you don't know the site, open `https://<tenant>.<wdN>.myworkdayjobs.com/robots.txt`: the site names are listed there.
- When a Workday site has no country filter, the scraper matches the country name in the job location as a whole word
  (so "India" does not match "Indiana").
  Add `"location_hints": ["Hyderabad"]` when the site lists cities without "India".
- A company with several career sites can appear several times with the same name (for example Intertek).

## Verify the next sector

Only product companies whose career site actually returns India jobs go into `companies.json`.

```bash
python3 tools/build_candidates.py                 # after editing sectors/policy.json or the source list
python3 tools/discover.py healthcare_pharma       # guesses ATS ids, counts India jobs (about 2 min), writes discovery.json
python3 tools/discover_sites.py healthcare_pharma    # 2nd stage: guesses career hosts, finds SuccessFactors / Oracle / Eightfold / Phenom / Jibe sites (discovery2.json)
python3 tools/apply_discovery2.py healthcare_pharma --write   # dry run without --write; --accept "Name,Name" for the "check" lines
python3 tools/sniff.py https://careers.example.com   # for a single company: which platform?
# put the verified entries into sectors/<sector>/companies.json (same format as fintech / energy)
python3 tools/verify.py healthcare_pharma         # India jobs per company, errors; prune.py removes the zero ones
python3 scrape_sector.py healthcare_pharma --show-all --open
```

Not scrapable (kept out, or as `manual` links when India jobs are confirmed): Avature and Taleo sites, sites behind
a bot challenge (SAP, for example) and sites that rate-limit a full scan (Microsoft returns HTTP 429).

## Unit tests

Offline, standard library only, no network:

```bash
python3 -m unittest discover -s tests -v
```

They cover the text/location helpers, the Workday and SuccessFactors parsers, HTTP retries and closing of
error sockets, CSV/Markdown output, a full `main()` run with fake fetchers (retry and failure report), and a
check of every `sectors/*/companies.json` (known ATS, required fields, no shared tenant).


## Limits

- Workday only gives relative ages ("Posted 4 Days Ago"), so ages are approximate and "30+ Days Ago" counts as 30.
- Keyword search is full text, so some results mention Kubernetes only in the description.
- Hitachi Energy's career site is the whole Hitachi group's, so some results are from other Hitachi companies.
- Sites can change their site name or block bots. A failing company is reported at the end of the run and does not stop the others.
