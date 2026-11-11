#!/usr/bin/env python3
"""Daily scraper for Kubernetes / DevOps / SRE jobs at MNC career sites (currently the fintech list).

Supported career-site platforms: Workday, Oracle HCM, Greenhouse, Jibe (iCIMS), SuccessFactors
RSS, Eightfold, Phenom, Radancy/TalentBrew, iCIMS portals, BrassRing, Goldman Sachs and D. E. Shaw.
Companies on other platforms are listed under "manual" in companies.json and are
printed at the end of the report as links to check by hand.

Usage:
  python3 scrape_jobs.py                      # India, last 14 days, only jobs not seen before
  python3 scrape_jobs.py --country Singapore --country-code SG
  python3 scrape_jobs.py --companies citi,visa --show-all
  python3 scrape_jobs.py --title-only         # only DevOps/SRE/platform-style titles

Only the Python standard library is used. Run it daily with cron, e.g.:
  0 9 * * * cd /path/to/mncs-jobs-scraper && /usr/bin/python3 scrape_jobs.py >> run.log 2>&1
"""
import argparse
import concurrent.futures as cf
import copy
import csv
import html as htmllib
import json
import logging
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# Terms sent to each site's search. Narrow, DevOps-specific terms keep the number of requests low.
DEFAULT_KEYWORDS = [
    "SRE", "Site Reliability", "DevOps", "Platform Engineering", "Kubernetes", "OpenShift",
    "DevSecOps", "GitOps", "CI/CD", "Infrastructure as Code", "Observability", "MLOps", "LLMOps",
    "Generative AI", "AI Infrastructure", "AI/ML"]
# Terms matched locally against each found job's title and description (no extra searches).
DEFAULT_SKILLS = [
    "AWS", "Azure", "Docker", "Linux", "Terraform", "Ansible", "Jenkins", "GitLab CI/CD",
    "GitHub Actions", "ArgoCD", "Helm", "Python", "Shell Scripting", "Prometheus", "Grafana",
    "OpenTelemetry", "ELK", "Splunk", "Dynatrace", "Datadog", "Jaeger", "Istio", "Kafka",
    "Microservices", "Automation", "Monitoring", "Incident Management", "Production Support",
    "Troubleshooting", "GPU Computing"]

# A title matching this is a strong hit; otherwise the keyword only appears in the description.
TITLE_RE = re.compile(
    r"devops|dev ops|devsecops|\bsre\b|site reliab|reliab|kubernetes|k8s|openshift|container|"
    r"platform eng|cloud (eng|ops|infra|native|platform)|infrastructure|release eng|"
    r"production support|systems? (eng|op)|observab|ci/?cd|sysops|"
    r"ai/?ml|ml ?ops|machine learning|\bml\b|\bai\b", re.I)

log = logging.getLogger("jobs")


# --------------------------------------------------------------------------- helpers
def http_get(url, body=None, headers=None, retries=3, timeout=30):
    """GET (or POST when body is given) with retries; returns the raw response bytes."""
    data = json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": UA}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404, 422):  # retrying will not help
                raise
            err = e
        except Exception as e:  # network hiccup, timeout
            err = e
        time.sleep(2 ** attempt)
    raise err


def http_json(url, body=None, headers=None, **kw):
    return json.loads(http_get(url, body, {"Accept": "application/json", **(headers or {})}, **kw))


def http_text(url, headers=None, **kw):
    return http_get(url, None, {"Accept": "text/html,application/xml,*/*", **(headers or {})},
                    **kw).decode("utf-8", "replace")


def age_from_text(text):
    """'Posted Today' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago' -> approximate age in days."""
    t = (text or "").lower()
    if "today" in t:
        return 0
    if "yesterday" in t:
        return 1
    m = re.search(r"(\d+)", t)
    return int(m.group(1)) if m else None


def age_from_iso(text):
    try:
        return (date.today() - datetime.strptime(text[:10], "%Y-%m-%d").date()).days
    except (TypeError, ValueError):
        return None


def has_term(text, term):
    """Case-insensitive whole-word match, so 'Helm' does not hit 'overwhelming'."""
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", text or "",
                     re.I) is not None


def make_job(company, platform, title, location, posted, age, url, job_id, term, text="",
             detail=""):
    return {
        "company": company, "platform": platform, "title": (title or "").strip(),
        "location": location or "", "posted": posted or "", "age_days": age,
        "relevance": "title" if TITLE_RE.search(title or "") or has_term(title, term) else "description",
        "url": url, "job_id": str(job_id or ""), "matched": {term},
        "text": text or "", "detail": detail,  # description text / API url used for skill matching
    }


def merge(jobs, job):
    """Same job can match several keywords: keep one record and union the keywords."""
    if job["url"] in jobs:
        jobs[job["url"]]["matched"] |= job["matched"]
        if job["relevance"] == "title":
            jobs[job["url"]]["relevance"] = "title"
        if not jobs[job["url"]]["text"]:
            jobs[job["url"]]["text"] = job["text"]
    else:
        jobs[job["url"]] = job


INDIA_CITIES = ["Bengaluru", "Bangalore", "Bangaluru", "Hyderabad", "Pune", "Chennai", "Mumbai", "Gurgaon",
                "Gurugram", "Noida", "Delhi", "Kolkata", "Ahmedabad", "Kochi", "Trivandrum",
                "Thiruvananthapuram", "Coimbatore", "Jaipur", "Indore", "Mohali", "Chandigarh",
                "Vadodara", "Nagpur", "Bhubaneswar", "IND-"]
COUNTRY_HINTS = {"india": INDIA_CITIES}


def loc_ok(text, a, extra=()):
    """True when a free-text location mentions the country, or one of its known cities."""
    t = (text or "").lower()
    hints = [a.country] + COUNTRY_HINTS.get(a.country.lower(), []) + list(extra)
    return any(h.lower() in t for h in hints)


CITY_ALIASES = {"bangalore": "Bengaluru", "bangaluru": "Bengaluru", "gurgaon": "Gurugram", "trivandrum": "Thiruvananthapuram"}


def cities_of(location, a):
    """Canonical city names found in a free-text location (India only); [] when none is named."""
    if a.country.lower() != "india":
        return []
    t = (location or "").lower()
    found = {CITY_ALIASES.get(c.lower(), c) for c in INDIA_CITIES
             if c != "IND-" and re.search(r"\b" + c.lower() + r"\b", t)}
    return sorted(found)


def kw_hits(text, a):
    return [k for k in a.keywords if has_term(text, k)]


def strip_tags(s):
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


# --------------------------------------------------------------------------- Workday
def find_country_facet(groups, country):
    """Locate (facetParameter, id) of the exact country value, searching nested facet groups."""
    for g in groups or []:
        for v in g.get("values", []):
            if "values" in v:
                found = find_country_facet([v], country)
                if found:
                    return found
            elif v.get("descriptor", "").strip().lower() == country.lower() and "id" in v:
                return g["facetParameter"], v["id"]
    return None


def fetch_workday(c, a):
    host, tenant, site = c["host"], c["tenant"], c["site"]
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    web = (f"https://{host}/recruiting/{tenant}/{site}" if "myworkdaysite" in host
           else f"https://{host}/en-US/{site}")
    probe = http_json(api, {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    facet = find_country_facet(probe.get("facets"), a.country)
    applied = {facet[0]: [facet[1]]} if facet else {}
    if not facet:
        log.debug("%s: no '%s' location facet, falling back to text match", c["name"], a.country)
    jobs = {}
    for term in a.keywords:
        offset, total = 0, None
        while offset < a.max_per_term:
            r = http_json(api, {"appliedFacets": applied, "limit": 20, "offset": offset,
                                "searchText": term})
            if total is None:  # Workday only reports 'total' on the first page
                total = r.get("total", 0)
            postings = r.get("jobPostings", [])
            for p in postings:
                if "title" not in p or "externalPath" not in p:  # Workday placeholder entry
                    continue
                loc = p.get("locationsText", "")
                if not facet and not loc_ok(loc + " " + p["externalPath"], a,
                                            c.get("location_hints", [])):
                    continue
                posted = p.get("postedOn", "")
                merge(jobs, make_job(c["name"], "workday", p["title"], loc,
                                     posted.replace("Posted ", ""), age_from_text(posted),
                                     web + p["externalPath"],
                                     (p.get("bulletFields") or [""])[0], term,
                                     detail=f"https://{host}/wday/cxs/{tenant}/{site}"
                                            + p["externalPath"]))
            offset += 20
            if not postings or offset >= total:
                break
    return list(jobs.values())


# --------------------------------------------------------------------------- Oracle HCM
def fetch_oracle(c, a):
    host, site = c["host"], c["site"]
    jobs = {}
    for term in a.keywords:
        offset, total = 0, None
        while offset < a.max_per_term:
            finder = (f'findReqs;siteNumber={site},keyword="{term}",limit=25,'
                      f'offset={offset},sortBy=POSTING_DATES_DESC')
            url = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
                   f"?onlyData=true&expand=requisitionList.secondaryLocations"
                   f"&finder={urllib.parse.quote(finder)}")
            items = http_json(url).get("items") or [{}]
            item = items[0]
            if total is None:
                total = item.get("TotalJobsCount", 0)
            reqs = item.get("requisitionList", [])
            for r in reqs:
                locs = [r.get("PrimaryLocation", "")] + [
                    s.get("Name", "") for s in r.get("secondaryLocations", [])]
                in_country = (r.get("PrimaryLocationCountry") == a.country_code or
                              any(a.country.lower() in l.lower() for l in locs))
                if not in_country:
                    continue
                posted = r.get("PostedDate", "")
                merge(jobs, make_job(
                    c["name"], "oracle", r["Title"], "; ".join(l for l in locs if l), posted,
                    age_from_iso(posted),
                    f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{r['Id']}",
                    r["Id"], term, text=r.get("ShortDescriptionStr") or ""))
            offset += 25
            if not reqs or offset >= total:
                break
    return list(jobs.values())


# --------------------------------------------------------------------------- Greenhouse
def fetch_greenhouse(c, a):
    data = http_json(f"https://boards-api.greenhouse.io/v1/boards/{c['board']}/jobs?content=true")
    jobs = {}
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        if not loc_ok(loc, a):
            continue
        text = j["title"] + " " + (j.get("content") or "")
        posted = j.get("first_published") or j.get("updated_at") or ""
        for term in a.keywords:
            if has_term(text, term):
                merge(jobs, make_job(c["name"], "greenhouse", j["title"], loc, posted[:10],
                                     age_from_iso(posted), j["absolute_url"], j["id"], term,
                                     text=strip_tags(htmllib.unescape(j.get("content") or ""))))
    return list(jobs.values())


# --------------------------------------------------------------------------- iCIMS / Jibe
def fetch_jibe(c, a):
    """Jibe career sites (Aon, Gallagher, ICE, AXA): public /api/jobs JSON search."""
    host, jobs = c["host"], {}
    for term in a.keywords:
        page = 1
        while (page - 1) * 100 < a.max_per_term:
            q = urllib.parse.urlencode({"keywords": term, "location": a.country,
                                        "page": page, "limit": 100})
            r = http_json(f"https://{host}/api/jobs?{q}")
            rows = r.get("jobs", [])
            for row in rows:
                d = row["data"]
                loc = d.get("full_location") or ""
                if d.get("country_code") != a.country_code and a.country.lower() not in (
                        (d.get("country") or "") + " " + loc).lower():
                    continue
                posted = d.get("posted_date") or ""
                merge(jobs, make_job(
                    c["name"], "jibe", d["title"], loc, posted[:10], age_from_iso(posted),
                    f"https://{host}/{c.get('job_path', 'jobs')}/{d['slug']}?lang=en-us",
                    d.get("req_id") or d["slug"], term,
                    text=strip_tags(" ".join(d.get(k) or "" for k in (
                        "description", "responsibilities", "qualifications")))))
            if len(rows) < 100 or page * 100 >= r.get("totalCount", 0):
                break
            page += 1
    return list(jobs.values())


# --------------------------------------------------------------------------- SuccessFactors RSS
def fetch_sf_rss(c, a):
    """SuccessFactors career sites (ANZ, Standard Chartered, Swiss Re, Worldline): RSS search."""
    host, jobs = c["host"], {}
    for term in a.keywords:
        q = urllib.parse.quote(f"({term}) AND locationSearch:({a.country})", safe="():")
        root = ET.fromstring(http_get(f"https://{host}/services/rss/job/?locale=en_GB&keywords={q}"))
        for item in root.iter("item"):
            pub = (item.findtext("pubDate") or "").strip()
            m = re.search(r"\d{1,2} \w{3} \d{4}", pub)
            if not m:  # placeholder item such as "No jobs currently available"
                continue
            posted = datetime.strptime(m.group(0), "%d %b %Y").date()
            title = (item.findtext("title") or "").strip()
            loc = re.search(r"\(([^()]*)\)\s*$", title)
            loc = loc.group(1) if loc else ""
            if not loc_ok(loc, a, [", " + a.country_code]) and not loc.endswith(a.country_code):
                continue
            url = (item.findtext("link") or "").split("?")[0]
            merge(jobs, make_job(c["name"], "sf_rss", re.sub(r"\s*\([^()]*\)\s*$", "", title),
                                 loc, posted.isoformat(), (date.today() - posted).days, url,
                                 url.rstrip("/").rsplit("/", 1)[-1], term,
                                 text=strip_tags(item.findtext("description") or "")))
    return list(jobs.values())


# --------------------------------------------------------------------------- Eightfold
def fetch_eightfold(c, a):
    """HSBC: Eightfold 'apply' API."""
    host, jobs = c["host"], {}
    for term in a.keywords:
        start, total = 0, None
        while start < a.max_per_term:
            q = urllib.parse.urlencode({"domain": c["domain"], "start": start, "num": 50,
                                        "query": term, "location": a.country,
                                        "sort_by": "relevance"})
            r = http_json(f"https://{host}/api/apply/v2/jobs?{q}")
            total = r.get("count", 0)
            rows = r.get("positions", [])
            for p in rows:
                locs = "; ".join(p.get("locations") or [p.get("location", "")])
                if not loc_ok(locs, a):
                    continue
                created = date.fromtimestamp(p["t_create"]) if p.get("t_create") else None
                merge(jobs, make_job(
                    c["name"], "eightfold", p["name"], locs,
                    created.isoformat() if created else "",
                    (date.today() - created).days if created else None,
                    p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{p['id']}",
                    p.get("display_job_id") or p["id"], term))
            start += 50
            if not rows or start >= total:
                break
    return list(jobs.values())


# --------------------------------------------------------------------------- Phenom
def fetch_phenom(c, a):
    """Allianz: Phenom People 'widgets' search API."""
    host, jobs = c["host"], {}
    for term in a.keywords:
        start, size = 0, 50
        while start < a.max_per_term:
            body = {"lang": c.get("lang", "en_global"), "deviceType": "desktop",
                    "country": "global", "pageName": "search-results", "ddoKey": "refineSearch",
                    "sortBy": "Most recent", "subsearch": "", "from": start, "jobs": True,
                    "counts": True, "all_fields": ["category", "country", "state", "city", "type"],
                    "size": size, "clearAll": False, "jdsource": "facets", "isSliderEnable": False,
                    "pageId": c.get("page_id", "page11"), "siteType": "external",
                    "keywords": term, "global": True,
                    "selected_fields": {"country": [a.country]}, "locationData": {}}
            d = http_json(f"https://{host}/widgets", body)["refineSearch"]
            rows = d.get("data", {}).get("jobs", [])
            for j in rows:
                if (j.get("country") or "").lower() != a.country.lower():
                    continue
                posted = j.get("postedDate") or ""
                merge(jobs, make_job(
                    c["name"], "phenom", j["title"], j.get("location", ""), posted[:10],
                    age_from_iso(posted),
                    f"https://{host}/{c.get('region', 'global/en')}/job/{j['jobSeqNo']}",
                    j.get("reqId") or j.get("jobId"), term,
                    text=strip_tags(j.get("descriptionTeaser") or "")))
            start += size
            if not rows or start >= d.get("totalHits", 0):
                break
    return list(jobs.values())


# --------------------------------------------------------------------------- Goldman Sachs
GS_QUERY = """query GetRoles($searchQueryInput: RoleSearchQueryInput!) {
  roleSearch(searchQueryInput: $searchQueryInput) {
    totalCount
    items { roleId jobTitle jobFunction division locations { primary state country city } }
  }
}"""


def fetch_goldman(c, a):
    """Goldman Sachs: higher.gs.com GraphQL gateway (no posting date is exposed)."""
    jobs = {}
    for term in a.keywords:
        page, size = 0, 20
        while page * size < a.max_per_term:
            variables = {"searchQueryInput": {
                "page": {"pageSize": size, "pageNumber": page},
                "sort": {"sortStrategy": "RELEVANCE", "sortOrder": "DESC"},
                "filters": [{"filterCategoryType": "LOCATION",
                             "filters": [{"filter": a.country, "subFilters": []}]}],
                "experiences": ["PROFESSIONAL", "EARLY_CAREER"], "searchTerm": term}}
            r = http_json("https://api-higher.gs.com/gateway/api/v1/graphql",
                          {"query": GS_QUERY, "variables": variables})
            res = r["data"]["roleSearch"]
            for it in res["items"]:
                locs = [l for l in it.get("locations") or [] if l.get("country")]
                if locs and not any(a.country.lower() in l["country"].lower() for l in locs):
                    continue
                loc = "; ".join(", ".join(x for x in (l.get("city"), l.get("state"),
                                                       l.get("country")) if x) for l in locs)
                rid = it["roleId"]
                merge(jobs, make_job(c["name"], "goldman", it["jobTitle"], loc, "", None,
                                     f"https://higher.gs.com/roles/{rid.split('_')[0]}",
                                     rid, term))
            page += 1
            if not res["items"] or page * size >= res["totalCount"]:
                break
    return list(jobs.values())


# --------------------------------------------------------------------------- Radancy / TalentBrew
def fetch_radancy(c, a):
    """Moody's, Citizens: TalentBrew search-results fragment (the Location filter is ignored by
    the server, so the country is matched client side)."""
    host, path = c["host"], c["path"]
    jobs = {}
    for term in a.keywords:
        page, pages = 1, 1
        while page <= pages and (page - 1) * 100 < a.max_per_term:
            q = urllib.parse.urlencode({
                "CurrentPage": page, "RecordsPerPage": 100, "Keywords": term, "Location": "",
                "Distance": 50, "SortCriteria": 0, "SortDirection": 0,
                "SearchResultsModuleName": "Search Results",
                "SearchFiltersModuleName": "Search Filters"})
            frag = http_json(f"https://{host}/{path}/results?{q}",
                             headers={"X-Requested-With": "XMLHttpRequest"}).get("results", "")
            m = re.search(r'data-total-pages="(\d+)"', frag)
            pages = int(m.group(1)) if m else 1
            for m in re.finditer(r'<a\s+href="(/[^"]*job/[^"]+)"[^>]*?data-job-id="(\d+)"[^>]*>(.*?)</a>',
                                 frag, re.S):
                href, jid, inner = m.groups()
                t = re.search(r"<h2[^>]*>(.*?)</h2>", inner, re.S)
                l = re.search(r'class="job-location"[^>]*>(.*?)</span>', inner, re.S)
                d = re.search(r'class="job-date-posted"[^>]*>(.*?)</span>', inner, re.S)
                loc = strip_tags(l.group(1)) if l else ""
                if not loc_ok(loc, a, c.get("location_hints", [])):
                    continue
                posted = age = None
                if d:
                    try:
                        dt = datetime.strptime(strip_tags(d.group(1)), "%m/%d/%Y").date()
                        posted, age = dt.isoformat(), (date.today() - dt).days
                    except ValueError:
                        pass
                merge(jobs, make_job(c["name"], "radancy", strip_tags(t.group(1)) if t else "",
                                     loc, posted, age, f"https://{host}{href}", jid, term))
            page += 1
    return list(jobs.values())


# --------------------------------------------------------------------------- iCIMS portal
def fetch_icims(c, a):
    """MSCI: classic iCIMS portals (server-rendered results, locations like 'IN-KA-Bengaluru')."""
    jobs = {}
    for host in c["hosts"]:
        for term in a.keywords:
            for pr in range(max(1, a.max_per_term // 20)):
                q = urllib.parse.urlencode({"ss": 1, "searchKeyword": term,
                                            "searchRelation": "keyword_all", "pr": pr,
                                            "in_iframe": 1})
                page = http_text(f"https://{host}/jobs/search?{q}")
                cards = page.split('class="iCIMS_JobCardItem"')[1:]
                for card in cards:
                    link = re.search(r'<a href="([^"]+/jobs/(\d+)/[^"]+)"[^>]*>.*?<h3[^>]*>(.*?)</h3>',
                                     card, re.S)
                    loc = re.search(r'Job Locations</span>.*?<span\s*>(.*?)</span>', card, re.S)
                    if not link:
                        continue
                    loc = strip_tags(loc.group(1)) if loc else ""
                    if not (loc.startswith(a.country_code + "-") or loc_ok(loc, a)):
                        continue
                    d = re.search(r'<span title="(\d+/\d+/\d{4})', card)
                    posted = age = None
                    if d:
                        dt = datetime.strptime(d.group(1), "%m/%d/%Y").date()
                        posted, age = dt.isoformat(), (date.today() - dt).days
                    merge(jobs, make_job(c["name"], "icims", strip_tags(link.group(3)), loc,
                                         posted, age, link.group(1).split("?")[0],
                                         link.group(2), term))
                if len(cards) < 20:  # iCIMS pages hold 20 results; a short page is the last one
                    break
    return list(jobs.values())


# --------------------------------------------------------------------------- BrassRing (UBS)
def fetch_brassring(c, a):
    """UBS: BrassRing 'TGnewUI'. The home page hands out a session token and cookies that the
    PowerSearchJobs endpoint needs; the keyword search is loose, so hits are re-checked locally."""
    import http.cookiejar
    host, pid, sid = c["host"], c["partner_id"], c["site_id"]
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    home = (f"https://{host}/TGnewUI/Search/home/HomeWithPreLoad?partnerid={pid}&siteid={sid}"
            "&PageType=searchResults&SearchType=linkquery")

    def call(url, body=None, headers=None):
        req = urllib.request.Request(
            url, data=json.dumps(body).encode() if body else None,
            headers={"User-Agent": UA, **(headers or {})})
        with opener.open(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")

    page = htmllib.unescape(call(home))
    tok = re.search(r'__RequestVerificationToken" type="hidden" value="([^"]+)"', page)
    sess = re.search(r'EncryptedSessionValue\\":\\"([^"\\]+)', page)
    kwf = re.search(r'KeywordCustomSolrFields\\":\\"([^"\\]+)', page)
    lcf = re.search(r'LocationCustomSolrFields\\":\\"([^"\\]+)', page)
    if not (tok and sess and kwf and lcf):
        raise RuntimeError("BrassRing session token not found on the home page")
    jobs, seen = {}, set()
    for pn in range(1, 41):  # all jobs in the country, newest first; keywords are matched locally
        body = {"partnerId": str(pid), "siteId": str(sid), "keyword": "",
                "location": a.country, "keywordCustomSolrFields": kwf.group(1),
                "locationCustomSolrFields": lcf.group(1), "linkId": "", "turnOffHttps": False,
                "Latitude": 0, "Longitude": 0, "encryptedSessionValue": sess.group(1),
                "pageNumber": pn, "facetfilterfields": {"Facet": []},
                "powersearchoptions": {"PowerSearchOption": []}, "SortType": "LastUpdated",
                "jobSiteIds": "", "encryptedSessionValue2": ""}
        r = json.loads(call(f"https://{host}/TgNewUI/Search/Ajax/PowerSearchJobs", body, {
            "Content-Type": "application/json;charset=UTF-8", "RFT": tok.group(1),
            "X-Requested-With": "XMLHttpRequest", "Referer": home}))
        rows = [row for row in r.get("Jobs", {}).get("Job", []) if row["Link"] not in seen]
        for row in rows:
            seen.add(row["Link"])
            f = {q["QuestionName"]: q["Value"] for q in row["Questions"]}
            title = htmllib.unescape(f.get("jobtitle", ""))
            loc = htmllib.unescape(f.get("formtext23", ""))
            if not loc_ok(loc, a):
                continue
            try:
                dt = datetime.strptime(f.get("lastupdated", ""), "%d-%b-%Y").date()
                posted, age = dt.isoformat(), (date.today() - dt).days
            except ValueError:
                posted = age = None
            if age is not None and age > a.max_age_days:  # newest first: the rest is older
                return list(jobs.values())
            desc = strip_tags(f.get("jobdescription", ""))
            for term in kw_hits(title + " " + desc, a):
                merge(jobs, make_job(c["name"], "brassring", title, loc, posted, age,
                                     row["Link"], f.get("reqid"), term, text=desc))
        if not rows:
            break
    return list(jobs.values())


# --------------------------------------------------------------------------- SmartRecruiters
def fetch_smartrecruiters(c, a):
    """Experian, Avaloq: public SmartRecruiters postings API (descriptions are fetched later)."""
    cid, jobs = c["company_id"], {}
    for term in a.keywords:
        offset = 0
        while offset < a.max_per_term:
            q = urllib.parse.urlencode({"q": term, "country": a.country_code.lower(),
                                        "limit": 100, "offset": offset})
            r = http_json(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings?{q}")
            rows = r.get("content", [])
            for p in rows:
                loc = p.get("location") or {}
                if (loc.get("country") or "").lower() != a.country_code.lower():
                    continue
                posted = p.get("releasedDate") or ""
                merge(jobs, make_job(
                    c["name"], "smartrecruiters", p["name"], loc.get("fullLocation", ""),
                    posted[:10], age_from_iso(posted),
                    f"https://jobs.smartrecruiters.com/{cid}/{p['id']}",
                    p.get("refNumber") or p["id"], term, detail=p.get("ref", "")))
            offset += 100
            if len(rows) < 100 or offset >= r.get("totalFound", 0):
                break
    return list(jobs.values())


# --------------------------------------------------------------------------- Lever
def fetch_lever(c, a):
    """Dun & Bradstreet: Lever postings API returns every posting with its description."""
    jobs = {}
    for p in http_json(f"https://api.lever.co/v0/postings/{c['company_id']}?mode=json"):
        cat = p.get("categories") or {}
        loc = "; ".join(cat.get("allLocations") or [cat.get("location") or ""])
        if not loc_ok(loc, a):
            continue
        text = p.get("descriptionPlain", "") + " " + " ".join(
            x.get("text", "") + " " + strip_tags(x.get("content", "")) for x in p.get("lists", []))
        created = date.fromtimestamp(p["createdAt"] / 1000) if p.get("createdAt") else None
        for term in kw_hits(p["text"] + " " + text, a):
            merge(jobs, make_job(
                c["name"], "lever", p["text"], loc, created.isoformat() if created else "",
                (date.today() - created).days if created else None, p["hostedUrl"], p["id"],
                term, text=text))
    return list(jobs.values())


# --------------------------------------------------------------------------- Bank of America
def fetch_bofa(c, a):
    """Bank of America: careers.bankofamerica.com search servlet. It exposes only the newest 100
    jobs of a location and no descriptions, so each recent job page is fetched and keyword-matched."""
    host = "careers.bankofamerica.com"
    q = urllib.parse.urlencode({"start": 0, "rows": 100, "search": "jobsByLocation",
                                "searchstring": a.country})
    rows = http_json(f"https://{host}/services/jobssearchservlet?{q}").get("jobsList") or []
    recent = []
    for r in rows:
        try:
            dt = datetime.strptime(r.get("postedDate") or "", "%m/%d/%Y").date()
        except ValueError:
            continue
        if (r.get("country") or "").lower() == a.country.lower() \
                and (date.today() - dt).days <= a.max_age_days:
            recent.append((r, dt))

    def one(item):
        r, dt = item
        try:
            page = http_text(f"https://{host}{r['jcrURL']}", retries=2, timeout=25)
            text = strip_tags(re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S))
        except Exception as e:
            log.debug("BofA description fetch failed for %s: %s", r["jcrURL"], e)
            text = ""
        return r, dt, text[:20000]

    jobs = {}
    with cf.ThreadPoolExecutor(4) as ex:
        for r, dt, text in ex.map(one, recent):
            loc = ", ".join(x for x in (r.get("city"), r.get("state"), r.get("country")) if x)
            for term in kw_hits(r["postingTitle"] + " " + text, a):
                merge(jobs, make_job(c["name"], "bofa", r["postingTitle"], loc, dt.isoformat(),
                                     (date.today() - dt).days, f"https://{host}{r['jcrURL']}",
                                     r["jobRequisitionId"], term, text=text))
    return list(jobs.values())


# --------------------------------------------------------------------------- D. E. Shaw
def fetch_deshaw(c, a):
    """D. E. Shaw: no search API, so the (~100) position pages are fetched and keyword-matched."""
    index = http_text("https://www.deshaw.com/careers")
    paths = sorted({p for p in re.findall(r'href="(/careers/[a-z0-9\-]+-\d{3,5})"', index)
                    if "all-positions" not in p})

    def one(path):
        page = http_text("https://www.deshaw.com" + path)
        t = re.search(r"<title>(.*?)</title>", page, re.S)
        title = strip_tags(t.group(1)).split(" | ")[0].split(" - ")[0] if t else path
        block = re.search(r'"jobLocations":\[(.*?)\]', page)
        locs = "; ".join(re.findall(r'"name":"([^"]+)"', block.group(1))) if block else ""
        text = strip_tags(page)[:20000]
        return path, title, locs, kw_hits(title + " " + text, a), text

    jobs = {}
    with cf.ThreadPoolExecutor(4) as ex:
        for path, title, locs, hits, text in ex.map(one, paths):
            if not hits or not loc_ok(locs, a):
                continue
            for term in hits:
                merge(jobs, make_job(c["name"], "deshaw", title, locs, "", None,
                                     "https://www.deshaw.com" + path, path.rsplit("-", 1)[-1],
                                     term, text=text))
    return list(jobs.values())


FETCHERS = {"workday": fetch_workday, "oracle": fetch_oracle, "greenhouse": fetch_greenhouse,
            "jibe": fetch_jibe, "sf_rss": fetch_sf_rss, "eightfold": fetch_eightfold,
            "phenom": fetch_phenom, "goldman": fetch_goldman, "radancy": fetch_radancy,
            "icims": fetch_icims, "brassring": fetch_brassring, "deshaw": fetch_deshaw,
            "smartrecruiters": fetch_smartrecruiters, "lever": fetch_lever, "bofa": fetch_bofa}


# --------------------------------------------------------------------------- state / output
def load_state(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_outputs(out_dir, jobs, manual):
    out_dir.mkdir(parents=True, exist_ok=True)
    fields = ["company", "title", "location", "posted", "age_days", "relevance", "matched",
              "platform", "job_id", "url", "first_seen"]
    with open(out_dir / "jobs.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for j in jobs:
            w.writerow({**j, "matched": ", ".join(sorted(j["matched"]))})
    lines = [f"# Kubernetes / DevOps / SRE jobs - {date.today()}", "",
             f"{len(jobs)} job(s).", "",
             "| Posted | Company | Role | Location | Match | Keywords |", "|---|---|---|---|---|---|"]
    for j in jobs:
        lines.append(f"| {j['posted']} | {j['company']} | [{j['title']}]({j['url']}) | "
                     f"{j['location']} | {j['relevance']} | {', '.join(sorted(j['matched']))} |")
    if manual:
        lines += ["", "## Not covered by the scraper (check manually)", ""]
        lines += [f"- {m['name']} ({m['platform']}): {m['url']}" for m in manual]
    (out_dir / "jobs.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_skills(jobs, a):
    """Tag each job with the skill terms found in its title/description; a skill in the title
    makes it a 'title' match. Workday descriptions are fetched here, one request per job."""
    def load(j):
        if j["detail"] and not j["text"]:
            try:
                data = http_json(j["detail"], retries=2, timeout=20)
                if "jobPostingInfo" in data:  # Workday
                    j["text"] = strip_tags(data["jobPostingInfo"].get("jobDescription", ""))
                else:  # SmartRecruiters
                    secs = (data.get("jobAd") or {}).get("sections") or {}
                    j["text"] = strip_tags(" ".join(s.get("text", "") for s in secs.values()))
            except Exception as e:  # skills just stay unmatched for this job
                log.debug("description fetch failed for %s: %s", j["url"], e)
    with cf.ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(load, jobs))
    for j in jobs:
        text = j["title"] + " " + j["text"]
        for s in a.skills:
            if has_term(text, s):
                j["matched"].add(s)
                if has_term(j["title"], s):
                    j["relevance"] = "title"


def split_terms(a, kind, parts=4):
    """Per-term search sites run the keyword list in parallel chunks; others run once."""
    if kind in ("greenhouse", "deshaw", "brassring", "lever", "bofa") or len(a.keywords) < 2 * parts:
        return [a]
    chunks = []
    for i in range(parts):
        b = copy.copy(a)
        b.keywords = a.keywords[i::parts]
        chunks.append(b)
    return chunks


def sort_key(j):
    return (j["relevance"] != "title", j["age_days"] if j["age_days"] is not None else 999,
            j["company"])


def print_table(headers, rows):
    """Print rows as a boxed text table sized to the longest value in each column."""
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)]
    line = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    fmt = "| " + " | ".join(f"{{:<{w}}}" for w in widths) + " |"
    print(line)
    print(fmt.format(*headers))
    print(line)
    for r in rows:
        print(fmt.format(*r))
    print(line)


def clip(text, n):
    return text if len(text) <= n else text[:n - 1] + "…"


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kubernetes / DevOps / SRE jobs - __TODAY__</title>
<style>
 body{font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;color:#1f2328;background:#f6f8fa}
 header{padding:16px 24px;background:#0f62fe;color:#fff}
 header h1{margin:0 0 4px;font-size:20px} header p{margin:0;opacity:.9}
 .bar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:12px 24px;background:#fff;
      border-bottom:1px solid #d0d7de;position:sticky;top:0;z-index:2}
 .bar input[type=search]{flex:1;min-width:220px;padding:7px 10px;border:1px solid #d0d7de;border-radius:6px}
 .bar select{padding:7px;border:1px solid #d0d7de;border-radius:6px}
 .count{margin-left:auto;color:#57606a}
 main{padding:16px 24px}
 table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #d0d7de}
 th,td{padding:8px 10px;border-bottom:1px solid #eaeef2;text-align:left;vertical-align:top}
 th{background:#eef1f4;cursor:pointer;white-space:nowrap;user-select:none}
 th:hover{background:#e1e6eb} tr:hover td{background:#f0f6ff}
 a{color:#0969da;text-decoration:none;font-weight:600} a:hover{text-decoration:underline}
 .tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px}
 .title{background:#dafbe1;color:#116329} .description{background:#fff8c5;color:#7d4e00}
 .new{background:#ffebe9;color:#a40e26;margin-left:6px;font-weight:600}
 .kw{color:#57606a;font-size:12px} .none{padding:24px;text-align:center;color:#57606a}
 h2{font-size:16px;margin:28px 0 8px} ul{margin:0;padding-left:20px} li{margin:3px 0}
</style></head><body>
<header><h1>Kubernetes / DevOps / SRE jobs</h1>
<p id="sub"></p></header>
<div class="bar">
 <input type="search" id="q" placeholder="Search role, company, location, keyword...">
 <select id="company"></select>
 <select id="city"></select>
 <select id="kwsel"></select>
 <select id="platform"></select>
 <select id="match"><option value="">All matches</option>
  <option value="title">Keyword in title</option><option value="description">Keyword in description only</option></select>
 <select id="age"><option value="">Any age</option><option value="1">Last 1 day</option>
  <option value="3">Last 3 days</option><option value="7">Last 7 days</option><option value="14">Last 14 days</option></select>
 <label title="Jobs whose posting date is today (jobs without a posting date are hidden)"><input type="checkbox" id="onlynew"> Posted today only <span id="newcount"></span></label>
 <span class="count" id="count"></span>
</div>
<main>
<table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table>
<div class="none" id="none" hidden>No jobs match the filters.</div>
<div id="manual"></div>
</main>
<script>
const D = __DATA__;
const COLS = [["Posted","age_days"],["Company","company"],["Role","title"],["Location","location"],
              ["Match","relevance"],["Keywords","kw"],["Platform","platform"]];
const $ = id => document.getElementById(id);
let sortKey = null, sortDir = 1;
D.jobs.forEach((j,i) => { j.i = i; j.kw = j.matched.join(", "); j.isNew = j.first_seen === D.today; });
$("newcount").textContent = `(${D.jobs.filter(j=>j.age_days === 0).length})`;
$("sub").textContent = `${D.jobs.length} matching jobs, ${D.jobs.filter(j=>j.age_days === 0).length} posted today, ${D.jobs.filter(j=>j.isNew).length} first seen by the scraper today (${D.today}). Generated ${D.generated}.`;
D.jobs.forEach(j => { j.cityList = j.cities.length ? j.cities : ["Other / not stated"]; });
function fill(id, all, values) {
  const sel = $(id);
  [["", all], ...[...new Set(values)].sort().map(v => [v, v])].forEach(([v,t]) => {
    const o = document.createElement("option"); o.value = v; o.textContent = t; sel.append(o); });
}
fill("company", "All companies", D.jobs.map(j => j.company));
fill("city", "All cities", D.jobs.flatMap(j => j.cityList));
fill("kwsel", "All keywords", D.jobs.flatMap(j => j.matched));
fill("platform", "All platforms", D.jobs.map(j => j.platform));
const head = $("head");
COLS.forEach(([label,key]) => { const th = document.createElement("th"); th.textContent = label;
  th.onclick = () => { sortDir = sortKey === key ? -sortDir : 1; sortKey = key; render(); }; head.append(th); });

function cell(tr, text, cls) { const td = document.createElement("td"); td.textContent = text; if (cls) td.className = cls; tr.append(td); return td; }
function render() {
  const q = $("q").value.toLowerCase().trim(), comp = $("company").value, m = $("match").value,
        city = $("city").value, kw = $("kwsel").value, plat = $("platform").value,
        age = parseInt($("age").value || "0"), onlyNew = $("onlynew").checked;
  let rows = D.jobs.filter(j =>
    (!comp || j.company === comp) && (!m || j.relevance === m) && (!onlyNew || j.age_days === 0) &&
    (!city || j.cityList.includes(city)) && (!kw || j.matched.includes(kw)) &&
    (!plat || j.platform === plat) &&
    (!age || (j.age_days !== null && j.age_days <= age)) &&
    (!q || (j.title + " " + j.company + " " + j.location + " " + j.cityList.join(" ") + " " + j.kw).toLowerCase().includes(q)));
  if (sortKey) rows.sort((a,b) => {
    let x = a[sortKey], y = b[sortKey];
    if (sortKey === "age_days") { x = x ?? 999; y = y ?? 999; }
    return (x < y ? -1 : x > y ? 1 : a.i - b.i) * sortDir; });
  const body = $("body"); body.innerHTML = "";
  rows.forEach(j => {
    const tr = document.createElement("tr");
    cell(tr, j.posted); cell(tr, j.company);
    const td = document.createElement("td");
    const a = document.createElement("a"); a.textContent = j.title;
    if (/^https?:\/\//.test(j.url)) { a.href = j.url; a.target = "_blank"; a.rel = "noopener noreferrer"; }
    td.append(a);
    if (j.isNew) { const n = document.createElement("span"); n.className = "tag new"; n.textContent = "NEW"; td.append(n); }
    tr.append(td); cell(tr, j.location);
    const mt = cell(tr, ""); const tag = document.createElement("span");
    tag.className = "tag " + j.relevance; tag.textContent = j.relevance; mt.append(tag);
    cell(tr, j.kw, "kw"); cell(tr, j.platform, "kw");
    body.append(tr); });
  $("count").textContent = `${rows.length} of ${D.jobs.length} jobs`;
  $("none").hidden = rows.length > 0;
}
["q","company","city","kwsel","platform","match","age","onlynew"].forEach(id => $(id).addEventListener(id === "q" ? "input" : "change", render));
if (D.manual.length) {
  const box = $("manual"), h = document.createElement("h2"), ul = document.createElement("ul");
  h.textContent = "Not covered by the scraper - check these career sites manually"; box.append(h, ul);
  D.manual.forEach(m => { const li = document.createElement("li"), a = document.createElement("a");
    a.textContent = m.name; a.href = m.url; a.target = "_blank"; a.rel = "noopener noreferrer";
    li.append(a, " (" + m.platform + ")"); ul.append(li); });
}
render();
</script></body></html>
"""


def write_html(path, jobs, manual, today, a):
    """Self-contained, sortable and filterable report with clickable links."""
    fields = ("company", "title", "location", "posted", "age_days", "relevance", "platform",
              "url", "first_seen")
    data = {"today": today, "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "manual": manual,
            "jobs": [{**{k: j.get(k) for k in fields}, "matched": sorted(j["matched"]),
                      "cities": cities_of(j["location"], a)}
                     for j in jobs]}
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")  # keeps </script> and <!-- out of the data
    html = HTML_TEMPLATE.replace("__TODAY__", today).replace("__DATA__", blob)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--country", default="India", help="location country name (default India)")
    ap.add_argument("--country-code", default="IN", help="ISO code used by Oracle HCM (default IN)")
    ap.add_argument("--keywords", default=",".join(DEFAULT_KEYWORDS),
                    help="comma separated search terms sent to each career site")
    ap.add_argument("--skills", default=",".join(DEFAULT_SKILLS),
                    help="comma separated terms matched against each found job's text "
                         "(no extra searches)")
    ap.add_argument("--max-age-days", type=int, default=14,
                    help="drop jobs older than this (Workday ages are approximate)")
    ap.add_argument("--title-only", action="store_true",
                    help="keep only jobs whose title looks DevOps/SRE/platform related")
    ap.add_argument("--show-all", action="store_true",
                    help="report every match, not only jobs not seen in earlier runs")
    ap.add_argument("--companies", default="", help="comma separated company-name filter")
    ap.add_argument("--config", default=str(BASE_DIR / "companies.json"))
    ap.add_argument("--output-dir", default=str(BASE_DIR / "output"))
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--max-per-term", type=int, default=200,
                    help="max results fetched per company and keyword")
    ap.add_argument("--open", action="store_true", help="open the HTML report in the browser")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    a.keywords = [k.strip() for k in a.keywords.split(",") if k.strip()]
    a.skills = [k.strip() for k in a.skills.split(",") if k.strip()]
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    cfg = json.loads(Path(a.config).read_text())
    wanted = {n.strip().lower() for n in a.companies.split(",") if n.strip()}
    tasks = [(kind, c) for kind in FETCHERS for c in cfg.get(kind, [])
             if not wanted or any(w in c["name"].lower() for w in wanted)]
    manual = [m for m in cfg.get("manual", [])
              if not wanted or any(w in m["name"].lower() for w in wanted)]
    if not tasks:
        sys.exit("No companies matched.")

    merged, failed = {}, []
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(FETCHERS[k], c, b): c["name"]
                for k, c in tasks for b in split_terms(a, k)}
        for fut in cf.as_completed(futs):
            name = futs[fut]
            try:
                res = fut.result()
                log.info("%s: %d match(es)", name, len(res))
                for j in res:
                    merge(merged, j)
            except Exception as e:
                if name not in failed:
                    failed.append(name)
                print(f"[error] {name}: {e}", file=sys.stderr)
    all_jobs = list(merged.values())

    jobs = [j for j in all_jobs
            if j["age_days"] is None or j["age_days"] <= a.max_age_days]
    add_skills(jobs, a)
    jobs = [j for j in jobs if not a.title_only or j["relevance"] == "title"]

    # state: url -> first_seen / last_seen, used to report only new jobs on later runs
    out_root = Path(a.output_dir)
    state_path = out_root / "seen_jobs.json"
    state = load_state(state_path)
    today = date.today().isoformat()
    for j in jobs:
        entry = state.setdefault(j["url"], {"first_seen": today})
        entry["last_seen"] = today
        j["first_seen"] = entry["first_seen"]
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    state = {u: e for u, e in state.items() if e.get("last_seen", today) >= cutoff}
    out_root.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=1))

    report = [j for j in jobs if a.show_all or j["first_seen"] == today]
    report.sort(key=sort_key)
    write_outputs(out_root / today, report, manual)
    # The HTML report lists every matching job and marks the ones first seen today as NEW.
    report_path = out_root / today / "report.html"
    write_html(report_path, sorted(jobs, key=sort_key), manual, today, a)
    (out_root / "latest.html").write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")

    print(f"{len(tasks)} sites scanned, {len(failed)} failed, {len(jobs)} matching job(s), "
          f"{len(report)} {'in report' if a.show_all else 'new'}.")
    if report:
        print_table(
            ["#", "Posted", "Company", "Role", "Location", "Match", "Keywords"],
            [[i, j["posted"], j["company"], clip(j["title"], 60), clip(j["location"], 30),
              j["relevance"], ", ".join(sorted(j["matched"]))] for i, j in enumerate(report, 1)])
        print("\nLinks:")
        for i, j in enumerate(report, 1):
            print(f"{i:>3}. {j['url']}")
    print(f"\nMatch = 'title' when the keyword is in the job title. "
          f"Files: {out_root / today}/jobs.csv and jobs.md")
    print(f"Browser report: {report_path}")
    if a.open:
        webbrowser.open(report_path.resolve().as_uri())
    if failed:
        print("Failed:", ", ".join(sorted(failed)))


if __name__ == "__main__":
    main()
