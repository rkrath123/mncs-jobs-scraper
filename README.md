# MNC Kubernetes / DevOps / SRE job scraper

**Quick start:** `python3 scrape_jobs.py --title-only --open` runs the scan and opens the browser report.
The report is also saved as `output/latest.html` (open it any time by double-clicking).

Searches the career sites of 59 fintech companies (56 scraped, 3 manual links) for Kubernetes, DevOps, SRE and OpenShift jobs
in India, and reports the jobs that appeared since the last run. Python 3 standard library only.

## Run

```bash
python3 scrape_jobs.py                  # new jobs from the last 14 days, India
python3 scrape_jobs.py --title-only     # only DevOps/SRE/platform-style titles (less noise)
python3 scrape_jobs.py --show-all       # everything that matches, not only new jobs
python3 scrape_jobs.py --companies citi,visa,lseg
python3 scrape_jobs.py --country Singapore --country-code SG
python3 scrape_jobs.py --keywords "kubernetes,terraform,sre" --max-age-days 7
python3 scrape_jobs.py --skills "Terraform,Python,AWS"   # tags only these skills on found jobs
```

Each run prints a table and writes `output/<date>/report.html` (also copied to `output/latest.html`),
`jobs.csv` and `jobs.md`.

The HTML report is a single file with clickable job links. It lists every matching job, marks the ones
first seen today as NEW, and has a search box, company / match / age filters, and sortable columns
(click a header). The "New today only" box is ticked by default when there are new jobs. It also lists
the career sites the scraper can't cover, as links.

`output/seen_jobs.json` remembers every job already reported, so the next day only shows new ones.
Running twice on the same day gives the same report. Delete `output/seen_jobs.json` to reset.

The Match column ("title" or "description") shows whether the keyword is in the job title.
`relevance=description` means it only appears in the job description, which is often noise.

## Keywords and skills

- **Keywords** (`--keywords`, 16 by default: SRE, Site Reliability, DevOps, Platform Engineering, Kubernetes,
  OpenShift, DevSecOps, GitOps, CI/CD, Infrastructure as Code, Observability, MLOps, LLMOps, Generative AI,
  AI Infrastructure, AI/ML) are searched on every career site. A job is only found if one of them matches.
- **Skills** (`--skills`, 30 by default: AWS, Azure, Docker, Linux, Terraform, Ansible, Jenkins, Python, Kafka,
  Prometheus, Grafana, Splunk, ...) are not searched. They are matched against the title and description of the
  jobs the keywords found, and show up in the Keywords column and the Keyword filter. A skill in the title makes
  the job a "title" match. This keeps the number of requests low. The catch: a job that mentions only a skill
  (for example Python) and none of the keywords is not found. Move a term into `--keywords` to search for it.
- Descriptions come from the career site's list API where it provides them, and from one extra request per job
  on Workday sites. Goldman Sachs, HSBC, Moody's, Citizens and MSCI give no description, so only their titles
  are checked for skills.

## Run every day (cron)

```bash
crontab -e
```
```
0 9 * * * cd /Users/rathram/Library/CloudStorage/OneDrive-IBM/Documents/mncs-jobs-scraper && /usr/bin/python3 scrape_jobs.py --title-only >> run.log 2>&1
```

## Coverage

| Platform | Companies | How |
|---|---|---|
| Workday | 34 sites (Wells Fargo, Citi, NatWest, LSEG, State Street, ...) | `wday/cxs` JSON API |
| Oracle HCM | JPMorganChase, Chubb, BNY, American Express | `hcmRestApi` JSON API |
| Greenhouse | Coinbase, Tide | `boards-api.greenhouse.io` |
| Jibe (iCIMS) | Aon, Gallagher, ICE, AXA | `/api/jobs` JSON API |
| SuccessFactors | ANZ, Standard Chartered, Swiss Re, Worldline | RSS search feed |
| Eightfold | HSBC | `/api/apply/v2/jobs` |
| Phenom | Allianz | `/widgets` search API |
| Radancy (TalentBrew) | Moody's, Citizens | search-results HTML fragment, country matched client side |
| iCIMS portal | MSCI | server-rendered search pages |
| BrassRing | UBS | `PowerSearchJobs` with the session token from the home page |
| In-house | Goldman Sachs (GraphQL), D. E. Shaw (scans position pages) | Goldman and D. E. Shaw give no posting date |
| Not covered | Revolut, BNP Paribas (block bots), Societe Generale (search service unreachable) | listed as links under `manual` in `companies.json` and in the report |

56 of the 59 companies are scanned. D. E. Shaw and Citizens currently have no roles in India, so they show no jobs.

Add a company by copying an entry in `companies.json`.

- Other platforms take the keys used in `companies.json` (for example `host`, or `hosts` for iCIMS).
- For Workday you need `host`, `tenant` and `site`. They appear in the career-site URL, for example
  `https://citi.wd5.myworkdayjobs.com/en-US/2/...` gives host `citi.wd5.myworkdayjobs.com`, tenant `citi`, site `2`.
- When a Workday site has no country filter, the scraper matches the country name in the job location.
  Add `"location_hints": ["Hyderabad"]` when the site lists cities without "India".

## Limits

- Workday only gives relative ages ("Posted 4 Days Ago"), so ages are approximate and "30+ Days Ago" counts as 30.
- Keyword search is full text, so some results mention Kubernetes only in the description.
- Sites can change their site name or block bots. A failing company is reported at the end of the run and does not stop the others.
