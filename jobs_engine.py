"""Shared engine for the sector job scrapers (fintech_scrape_jobs.py, energy_scrape_jobs.py, ...).

Scrapes jobs matching the keywords you pass with --keywords at MNC career sites. No keyword is
built in (nothing is hardcoded; the script asks for them when none are given). Each sector has its own
script (a few lines calling main()), its own company list and its own output folder.

Supported career-site platforms: Workday, Oracle HCM, Greenhouse, Lever, SmartRecruiters, Jibe
(iCIMS), SuccessFactors RSS, Eightfold, Phenom, Radancy/TalentBrew, iCIMS portals, BrassRing,
Goldman Sachs, D. E. Shaw, Bank of America, Avature, Cornerstone, Apple, Taleo, Google, Meta, IBM and SAP. Companies on other platforms are listed under
"manual" in the sector's company list and printed at the end of the report as links.

Only the Python standard library is used.
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
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
try:  # macOS allows only 256 open files by default; a full run opens many sockets
    import resource
    _soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(4096, _hard) if _hard > 0 else 4096, _hard))
except Exception:
    pass
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

# Extra title words (from --title-words). A job whose title matches one of them, or
# contains the searched keyword itself, is a "title" match; anything else only matched in the
# description. None until set_title_words() is called.
TITLE_RE = None


def set_title_words(words):
    """Compile title words: whole-word, case-insensitive; a trailing '*' means 'starts with'."""
    global TITLE_RE
    parts = [re.escape(w[:-1] if w.endswith("*") else w) + ("" if w.endswith("*") else r"(?![A-Za-z0-9])")
             for w in (x.strip() for x in words) if w and w != "*"]
    TITLE_RE = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + ")", re.I) if parts else None


def split_list(text):
    """'a, b,,c' -> ['a', 'b', 'c'] (comma separated; a slash is part of a term, e.g. CI/CD)."""
    return [x.strip() for x in (text or "").split(",") if x.strip()]


log = logging.getLogger("jobs")


# --------------------------------------------------------------------------- helpers
class SiteDown(Exception):
    """The site redirected to its own maintenance / outage page; retrying right away is pointless."""


OUTAGE_URL_RE = re.compile(r"maintenance|/drs/outage|outage\?", re.I)


def http_get(url, body=None, headers=None, retries=3, timeout=30):
    """GET (or POST when body is given) with retries; returns the raw response bytes."""
    data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
    h = {"User-Agent": UA}
    if data is not None:
        h["Content-Type"] = "application/json"
    h.update(headers or {})
    err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                final = r.geturl()
                if final != url and OUTAGE_URL_RE.search(final) and not OUTAGE_URL_RE.search(url):
                    raise SiteDown(f"site is down for maintenance (redirected to {final})")
                return r.read()
        except SiteDown:
            raise
        except urllib.error.HTTPError as e:
            e.close()  # an HTTPError keeps its socket open until garbage-collected: ~hundreds leak per run
            if e.code in (400, 401, 403, 404, 422):  # retrying will not help
                raise
            err = e
            if e.code == 429:  # rate limited: wait longer than the normal backoff (or as told)
                ra = e.headers.get("Retry-After", "") if e.headers else ""
                time.sleep(min(int(ra), 60) if ra.isdigit() else 10 * (attempt + 1))
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


LD_POSTED_RE = re.compile(r'"datePosted"\s*:\s*"(\d{4}-\d{2}-\d{2})')


def page_posted(page):
    """Posting date from the schema.org JobPosting block of a job page (None when absent)."""
    m = LD_POSTED_RE.search(page or "")
    return datetime.strptime(m.group(1), "%Y-%m-%d").date() if m else None


def has_term(text, term):
    """Case-insensitive whole-word match, so 'Helm' does not hit 'overwhelming'."""
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", text or "",
                     re.I) is not None


def make_job(company, platform, title, location, posted, age, url, job_id, term, text="",
             detail=""):
    return {
        "company": company, "platform": platform, "title": (title or "").strip(),
        "location": location or "", "posted": posted or "", "age_days": age,
        "relevance": "title" if (TITLE_RE and TITLE_RE.search(title or "")) or has_term(title, term)
                     else "description",
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
    # whole-word match, so 'India' does not hit 'Indiana' or 'Indianapolis'
    return any(re.search(r"(?<![a-z])" + re.escape(h.lower()) + r"(?![a-z])", t) for h in hints)


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


# Work mode. Title / location are trusted ("Remote - India"); descriptions only count for explicit
# phrases, because "remote servers" or "remote teams" would otherwise flag ordinary on-site jobs.
REMOTE_HEAD_RE = re.compile(r"\bremote(ly)?\b|work(ing)? from home|\bwfh\b|home[- ]?based|telecommut", re.I)
# Strong phrases make a job remote; a bare "work from home" only does when nothing in the text points to
# a hybrid / partly-in-office arrangement ("flexibility to work from home 1 day a week" is hybrid).
REMOTE_TEXT_RE = re.compile(r"\bfully remote\b|\bremote[- ](first|role|position|job|opportunity|work)\b|"
                            r"\b(this|is a|position is|role is) remote\b|\bremote within\b|telecommut", re.I)
REMOTE_WEAK_RE = re.compile(r"work(ing)? from home|\bwfh\b", re.I)
HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
PARTIAL_OFFICE_RE = re.compile(
    r"\b(?:in|at) the office\b.{0,40}\bdays?\b|\bdays?\b.{0,40}\b(?:in|at) the office\b|"
    r"\bdays? (?:a|per|each) week\b.{0,40}\b(?:home|office|on-?site)\b|"
    r"\b(?:home|office|on-?site)\b.{0,40}\bdays? (?:a|per|each) week\b", re.I)


def work_mode(job):
    """'remote', 'hybrid' or '' (on-site / not stated), from the job's title, location and description."""
    head = f"{job.get('title', '')} {job.get('location', '')}"
    text = job.get("text", "")
    if REMOTE_HEAD_RE.search(head) or REMOTE_TEXT_RE.search(text):
        return "remote"
    hybrid = HYBRID_RE.search(head) or HYBRID_RE.search(text) or PARTIAL_OFFICE_RE.search(text)
    if REMOTE_WEAK_RE.search(text) and not hybrid:
        return "remote"
    return "hybrid" if hybrid else ""


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
        raw = http_get(f"https://{host}/services/rss/job/?locale=en_GB&keywords={q}").decode("utf-8", "replace")
        # some feeds contain a bare "&" (e.g. "Festo SE & Co. KG"), which is invalid XML
        root = ET.fromstring(re.sub(r"&(?!(?:[A-Za-z]+|#\d+|#x[0-9A-Fa-f]+);)", "&amp;", raw).encode("utf-8"))
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
_pace_lock = threading.Lock()
_pace_next = {}  # host -> earliest time the next request may start


def pace(host, gap):
    """Space requests to one host `gap` seconds apart across all threads (a 429 costs far more
    than waiting). The slot is reserved under the lock, the sleep happens outside it."""
    if not gap:
        return
    with _pace_lock:
        now = time.monotonic()
        slot = max(now, _pace_next.get(host, 0))
        _pace_next[host] = slot + gap
    if slot > now:
        time.sleep(slot - now)


UNION_TERM = "(search)"  # keyword of a job found by a combined search whose title names none of them


def eightfold_pcsx(url, delay):
    """pcsx search; sites that rate-limit (Microsoft: HTTP 429 after 2-3 quick requests) set "delay":
    requests to the host are spaced `delay` seconds apart, and a 429 is still retried after a pause."""
    host = urllib.parse.urlsplit(url).netloc
    for attempt in range(6 if delay else 1):
        pace(host, delay)
        try:
            return http_json(url, retries=1 if delay else 3)
        except urllib.error.HTTPError as e:
            if e.code != 429 or not delay or attempt == 5:
                raise
            time.sleep(20 + 10 * attempt)


def fetch_eightfold(c, a):
    """Eightfold career sites: HSBC uses the 'apply' API; Eaton, Worley use 'pcsx' (set "api": "pcsx")."""
    host, jobs = c["host"], {}
    pcsx = c.get("api") == "pcsx"
    # A rate-limited site (Microsoft) gets ONE search for all keywords joined with OR (about 5 requests
    # instead of 2-3 per keyword); the keyword of each job is then read from its title.
    union = pcsx and c.get("delay") and len(a.keywords) > 1
    terms = [" OR ".join(f'"{k}"' if " " in k else k for k in a.keywords)] if union else a.keywords
    for term in terms:
        start, total = 0, None
        while start < a.max_per_term:
            if pcsx:
                q = urllib.parse.urlencode({"domain": c["domain"], "start": start,
                                            "query": term, "location": a.country})
                r = eightfold_pcsx(f"https://{host}/api/pcsx/search?{q}", c.get("delay", 0)).get("data") or {}
            else:
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
                ts = p.get("postedTs") or p.get("t_create")
                created = date.fromtimestamp(ts) if ts else None
                url = (p.get("canonicalPositionUrl")
                       or f"https://{host}{p.get('positionUrl') or '/careers/job/' + str(p['id'])}")
                for t in ((kw_hits(p["name"], a) or [UNION_TERM]) if union else [term]):
                    merge(jobs, make_job(
                        c["name"], "eightfold", p["name"], locs,
                        created.isoformat() if created else "",
                        (date.today() - created).days if created else None,
                        url, p.get("display_job_id") or p.get("displayJobId") or p["id"], t))
            start += len(rows) if pcsx else 50
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
                    "selected_fields": {"country": [c.get("country_value", a.country)]},  # some sites use "INDIA"
                    "locationData": {}}
            d = http_json(f"https://{host}/widgets", body)["refineSearch"]
            rows = d.get("data", {}).get("jobs", [])
            for j in rows:
                if (j.get("country") or "").lower() not in (a.country.lower(),
                                                           c.get("country_value", a.country).lower()):
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
    undated = [j for j in jobs.values() if not j["posted"]]  # the result list shows no date
    if undated:
        def read(j):
            try:
                return page_posted(http_text(j["url"] + "?in_iframe=1", retries=2, timeout=20))  # JSON-LD only in the iframe view
            except Exception as e:
                log.debug("posting date fetch failed for %s: %s", j["url"], e)
        with cf.ThreadPoolExecutor(8) as ex:
            for j, dt in zip(undated, ex.map(read, undated)):
                if dt:
                    j["posted"], j["age_days"] = dt.isoformat(), (date.today() - dt).days
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


# --------------------------------------------------------------------------- Amazon
def fetch_amazon(c, a):
    """Amazon: public amazon.jobs search.json (country filter takes the ISO-3 code, India = IND)."""
    iso3 = c.get("country3", "IND")
    jobs = {}
    for term in a.keywords:
        offset = 0
        while offset < a.max_per_term:
            q = urllib.parse.urlencode({"base_query": term, "country": iso3, "result_limit": 100,
                                        "offset": offset, "sort": "recent"})
            r = http_json(f"https://www.amazon.jobs/en/search.json?{q}")
            rows = r.get("jobs", [])
            for p in rows:
                if p.get("country_code") != iso3:
                    continue
                try:
                    dt = datetime.strptime(p.get("posted_date") or "", "%B %d, %Y").date()
                except ValueError:
                    dt = None
                loc = ", ".join(x for x in (p.get("city"), p.get("state"), a.country) if x)
                merge(jobs, make_job(
                    c["name"], "amazon", p["title"], loc, dt.isoformat() if dt else "",
                    (date.today() - dt).days if dt else None, "https://www.amazon.jobs" + p["job_path"],
                    p.get("id_icims") or p["id"], term,
                    text=strip_tags(" ".join(p.get(k) or "" for k in (
                        "description", "basic_qualifications", "preferred_qualifications")))))
            offset += 100
            if len(rows) < 100 or offset >= r.get("hits", 0):
                break
    return list(jobs.values())


# --------------------------------------------------------------------------- Atlassian
def fetch_atlassian(c, a):
    """Atlassian: one public JSON listing of all jobs (with descriptions), filtered locally."""
    jobs = {}
    for p in http_json("https://www.atlassian.com/endpoint/careers/listings"):
        locs = "; ".join(p.get("locations") or [])
        if not loc_ok(locs, a):
            continue
        text = strip_tags(" ".join(p.get(k) or "" for k in ("overview", "responsibilities", "qualifications")))
        url = (p.get("portalJobPost") or {}).get("portalUrl") or p["applyUrl"]
        for term in kw_hits(p["title"] + " " + text, a):
            merge(jobs, make_job(c["name"], "atlassian", p["title"], locs, "", None, url, p["id"], term,
                                 text=text))
    return list(jobs.values())


# --------------------------------------------------------------------------- Avature
AVATURE_ARTICLE_RE = re.compile(r"<article\b[^>]*>(.*?)(?=<article\b|</section>|\Z)", re.S)
AVATURE_LINK_RE = re.compile(r'<a\b[^>]*href="([^"]*/JobDetail/[^"]*)"[^>]*>(.*?)</a>', re.S)
AVATURE_POSTED_RE = re.compile(r"(?:Date )?Posted:?\s*(\d{1,2}-[A-Za-z]{3}-\d{4})")


def avature_date(text):
    m = AVATURE_POSTED_RE.search(text or "")
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%d-%b-%Y").date()
    except ValueError:
        return None


def avature_posting(url):
    """(location, posted) from a posting page: 'Country India City Bengaluru Date Posted 17-Apr-2026'."""
    page = http_text(url)
    text = strip_tags(page)
    m = re.search(r"Country\s+(.+?)\s+(?:City|State|Location)\s+(.+?)\s+(?:Date Posted|Job Category)", text)
    return ((", ".join(g.strip() for g in (m.group(2), m.group(1))) if m else ""),
            avature_date(text) or page_posted(page))


def fetch_avature(c, a):
    """Avature career sites (Synopsys, Lenovo, Tesco, EA): server-rendered SearchJobs pages. The
    free-text search also matches the location, so '<keyword> India' lists only India postings;
    when the list shows no location (Synopsys) the posting pages are read to confirm the country."""
    base, jobs, details = c["base"].rstrip("/"), {}, {}
    for term in a.keywords:
        offset, seen = 0, set()
        while offset < a.max_per_term:
            page = http_text(f"{base}/SearchJobs/{urllib.parse.quote(term + ' ' + a.country)}"
                             f"?jobOffset={offset}")
            rows = []
            for block in AVATURE_ARTICLE_RE.findall(page):
                link = AVATURE_LINK_RE.search(block)
                url = htmllib.unescape(link.group(1)) if link else ""
                if not url or url.split("?")[0] in seen:
                    continue
                seen.add(url.split("?")[0])
                loc = strip_tags((re.search(r'class="list-item-location"[^>]*>(.*?)</span>', block, re.S)
                                  or re.search(r'article__header__text__subtitle">\s*<span>(.*?)</span>',
                                               block, re.S) or [None, ""])[1])
                rows.append((block, url, strip_tags(link.group(2)), loc))
            todo = [u for b, u, _, loc in rows
                    if (not loc or (loc_ok(loc, a) and not avature_date(strip_tags(b))))
                    and u.split("?")[0] not in details]
            if todo:  # no location or date in the list: read the posting pages (in parallel)
                with cf.ThreadPoolExecutor(8) as ex:
                    for u, res in zip(todo, ex.map(avature_posting, todo)):
                        details[u.split("?")[0]] = res
            for block, url, title, loc in rows:
                key = url.split("?")[0]
                posted = avature_date(strip_tags(block))
                if key in details:
                    found_loc, found = details[key]
                    loc, posted = loc or found_loc, posted or found
                if not loc_ok(loc, a):
                    continue
                body = re.search(r'article__content">(.*?)</div>', block, re.S)
                merge(jobs, make_job(c["name"], "avature", title, loc,
                                     posted.isoformat() if posted else "",
                                     (date.today() - posted).days if posted else None,
                                     url, key.rstrip("/").rsplit("/", 1)[-1], term,
                                     text=strip_tags(body.group(1)) if body else ""))
            offset += len(rows)
            if not rows:
                break
    return list(jobs.values())


# --------------------------------------------------------------------------- Cornerstone
def fetch_cornerstone(c, a):
    """Cornerstone OnDemand career sites (Linde, Henkel): the career page hands out a bearer token
    for the rec-job-search API, which filters on the ISO country code."""
    host, site, corp = c["host"], c["site"], c["corp"]
    page = http_text(f"https://{host}/ux/ats/careersite/{site}/home?c={corp}")
    token = re.search(r'"token":"([^"]+)"', page).group(1)
    cloud = re.search(r'"cloud":"([^"]+)"', page).group(1)
    jobs = {}
    for term in a.keywords:
        number, total = 1, None
        while (number - 1) * 25 < a.max_per_term:
            body = {"careerSiteId": site, "careerSitePageId": site, "pageNumber": number, "pageSize": 25,
                    "cultureId": 1, "searchText": term, "cultureName": "en-US", "states": [],
                    "countryCodes": [a.country_code], "cities": [], "placeID": "", "radius": None,
                    "postingsWithinDays": None, "customFieldCheckboxKeys": [],
                    "customFieldDropdowns": [], "customFieldRadios": []}
            d = http_json(cloud + "rec-job-search/external/jobs", body,
                          {"Authorization": "Bearer " + token, "Content-Type": "application/json"})["data"]
            total = d.get("totalCount", 0)
            rows = d.get("requisitions", [])
            for r in rows:
                locs = "; ".join(", ".join(x for x in (l.get("city"), l.get("state"), a.country) if x)
                                 for l in r.get("locations") or [] if l.get("country") == a.country_code)
                posted = datetime.strptime(r["postingEffectiveDate"], "%m/%d/%Y").date() \
                    if r.get("postingEffectiveDate") else None
                rid = r["requisitionId"]
                merge(jobs, make_job(
                    c["name"], "cornerstone", r["displayJobTitle"], locs,
                    posted.isoformat() if posted else "",
                    (date.today() - posted).days if posted else None,
                    f"https://{host}/ux/ats/careersite/{site}/home/requisition/{rid}?c={corp}", rid, term,
                    text=strip_tags(r.get("externalDescription") or "")))
            number += 1
            if not rows or (number - 1) * 25 >= total:
                break
    return list(jobs.values())


# --------------------------------------------------------------------------- Apple
def fetch_apple(c, a):
    """Apple: public jobs.apple.com search API (postLocation-INDC is the India country filter)."""
    country = c.get("location", "postLocation-INDC")
    jobs = {}
    for term in a.keywords:
        page, seen = 1, 0
        while seen < a.max_per_term:
            r = http_json("https://jobs.apple.com/api/v1/search", {
                "query": term, "filters": {"locations": [country]}, "page": page, "locale": "en-us",
                "sort": "", "format": {"longDate": "MMMM D, YYYY", "mediumDate": "MMM D, YYYY"}})["res"]
            rows = r.get("searchResults", [])
            for p in rows:
                locs = "; ".join(l.get("name") or l.get("city") or "" for l in p.get("locations") or [])
                if not any(l.get("countryName") == a.country for l in p.get("locations") or []):
                    continue
                posted = (p.get("postDateInGMT") or "")[:10]
                merge(jobs, make_job(
                    c["name"], "apple", p["postingTitle"], f"{locs}, {a.country}", posted, age_from_iso(posted),
                    f"https://jobs.apple.com/en-in/details/{p['positionId']}/{p.get('transformedPostingTitle', '')}",
                    p.get("reqId") or p["positionId"], term, text=p.get("jobSummary") or ""))
            seen += len(rows)
            page += 1
            if not rows or seen >= r.get("totalRecords", 0):
                break
    return list(jobs.values())


# --------------------------------------------------------------------------- Taleo
def taleo_body(keyword, location_ids, page):
    def filters(*ids):
        return {"searchFilterSelections": [{"id": i, "selectedValues": location_ids if i == "LOCATION" and ids else []}
                                           for i in ids]}
    return {"multilineEnabled": False,
            "sortingSelection": {"sortBySelectionParam": "3", "ascendingSortingOrder": "false"},
            "fieldData": {"fields": {"KEYWORD": keyword, "LOCATION": ""}, "valid": True},
            "filterSelectionParam": filters("POSTING_DATE", "LOCATION", "JOB_FIELD", "JOB_TYPE", "JOB_SCHEDULE",
                                            "JOB_LEVEL"),
            "advancedSearchFiltersSelectionParam": {"searchFilterSelections": [
                {"id": i, "selectedValues": []} for i in ("ORGANIZATION", "LOCATION", "JOB_FIELD", "JOB_NUMBER",
                                                          "URGENT_JOB", "EMPLOYEE_STATUS", "STUDY_LEVEL",
                                                          "WILL_TRAVEL", "JOB_SHIFT")]},
            "pageNo": page}


def fetch_taleo(c, a):
    """Oracle Taleo Enterprise career sections (Mercedes-Benz): the career section page names the
    portal, and the REST search is narrowed with the country entry of the Location facet."""
    host, section = c["host"], c.get("section", "ex")
    base = f"https://{host}/careersection"
    portal = re.search(r"portal[^0-9]{0,6}(\d{6,})", http_text(f"{base}/{section}/jobsearch.ftl?lang=en")).group(1)
    api = f"{base}/rest/jobboard/searchjobs?lang=en&portal={portal}"
    hdr = {"tz": "GMT+05:30", "tzname": "Asia/Kolkata", "X-Requested-With": "XMLHttpRequest",
           "Referer": f"{base}/{section}/jobsearch.ftl"}
    facets = http_json(api, taleo_body("", [], 1), hdr).get("facetResults", [])
    ids = [v["id"] for f in facets if f["id"] == "LOCATION" for v in f["facetValueResults"]
           if v["text"].strip().lower() == a.country.lower()]
    if not ids:
        return []
    jobs = {}
    for term in a.keywords:
        page = 1
        while True:
            r = http_json(api, taleo_body(term, ids, page), hdr)
            rows, paging = r.get("requisitionList", []), r.get("pagingData", {})
            for p in rows:
                col = p.get("column", [])
                locs = "; ".join(json.loads(col[1])) if len(col) > 1 and col[1].startswith("[") else ""
                posted = ""
                if len(col) > 2:
                    try:
                        posted = datetime.strptime(col[2], "%b %d, %Y").date().isoformat()
                    except ValueError:
                        pass
                merge(jobs, make_job(c["name"], "taleo", col[0], locs, posted, age_from_iso(posted),
                                     f"{base}/{section}/jobdetail.ftl?job={p['contestNo']}&lang=en",
                                     p["contestNo"], term))
            if not rows or page * paging.get("pageSize", 25) >= min(paging.get("totalCount", 0), a.max_per_term):
                break
            page += 1
    return list(jobs.values())


# --------------------------------------------------------------------------- Google
def fetch_google(c, a):
    """Google (and DeepMind): the results page embeds the job list as JSON (20 per page)."""
    jobs = {}
    for term in a.keywords:
        page, seen = 1, 0
        while seen < a.max_per_term:
            q = urllib.parse.urlencode({"location": a.country, "q": term, "page": page})
            html = http_text(f"https://www.google.com/about/careers/applications/jobs/results/?{q}")
            m = re.search(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?), sideChannel", html, re.S)
            data = json.loads(m.group(1)) if m else [[], None, 0]
            rows = data[0] or []
            for p in rows:
                locs = "; ".join(l[0] for l in p[9] or [] if l and l[0])
                if not loc_ok(locs, a):
                    continue
                posted = date.fromtimestamp(p[12][0]) if len(p) > 12 and p[12] else None
                slug = re.sub(r"[^a-z0-9]+", "-", p[1].lower()).strip("-")
                text = " ".join(strip_tags(x[1]) for x in (p[3], p[4], p[10]) if x and len(x) > 1 and x[1])
                merge(jobs, make_job(
                    c["name"], "google", p[1], locs, posted.isoformat() if posted else "",
                    (date.today() - posted).days if posted else None,
                    f"https://www.google.com/about/careers/applications/jobs/results/{p[0]}-{slug}", p[0], term,
                    text=text))
            seen += len(rows)
            page += 1
            if not rows or seen >= (data[2] or 0):
                break
    return list(jobs.values())


# --------------------------------------------------------------------------- Meta
META_HEADERS = {"User-Agent": UA.replace(" Safari/537.36", "")}  # metacareers answers HTTP 400 to a UA with "Safari"
META_OFFICES = ["Bangalore, India", "Gurgaon, India", "Hyderabad, India", "Mumbai, India", "New Delhi, India"]


def fetch_meta(c, a):
    """Meta: metacareers.com GraphQL job search (one call returns every match; no posting date).
    The persisted query id is public but changes now and then: set "doc_id" in the company entry."""
    page = http_text("https://www.metacareers.com/jobs", META_HEADERS)
    lsd = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', page).group(1)
    jobs = {}
    for term in a.keywords:
        variables = {"search_input": {
            "q": term, "divisions": [], "offices": c.get("offices", META_OFFICES), "roles": [],
            "leadership_levels": [], "saved_jobs": [], "saved_searches": [], "sub_teams": [], "teams": [],
            "is_leadership": False, "is_remote_only": False, "sort_by_new": False, "page": 1,
            "results_per_page": None}}
        body = urllib.parse.urlencode({
            "lsd": lsd, "doc_id": c.get("doc_id", "9114524511922157"), "variables": json.dumps(variables),
            "fb_api_req_friendly_name": "CareersJobSearchResultsDataQuery"}).encode()
        raw = json.loads(http_get("https://www.metacareers.com/api/graphql/", body, {
            **META_HEADERS, "Content-Type": "application/x-www-form-urlencoded", "X-FB-LSD": lsd}))
        if "errors" in raw:
            raise RuntimeError(raw["errors"][0].get("message", "GraphQL error"))
        for p in raw["data"]["job_search"]:
            locs = "; ".join(p.get("locations") or [])
            if not loc_ok(locs, a):
                continue
            merge(jobs, make_job(c["name"], "meta", p["title"], locs, "", None,
                                 f"https://www.metacareers.com/jobs/{p['id']}", p["id"], term,
                                 text=" ".join((p.get("teams") or []) + (p.get("sub_teams") or []))))
    return list(jobs.values())


# --------------------------------------------------------------------------- IBM
def fetch_ibm(c, a):
    """IBM: the search API behind ibm.com/careers/search (country filter = field_keyword_05)."""
    jobs, size = {}, 50
    for term in a.keywords:
        start = 0
        while start < a.max_per_term:
            r = http_json("https://www-api.ibm.com/search/api/v2", {
                "appId": "careers", "scopes": ["careers2"],
                "query": {"bool": {"must": [{"simple_query_string": {"query": term, "fields": [
                    "keywords^1", "body^1", "url^2", "description^2", "h1s_content^2", "title^2",
                    "field_keyword_05^2"]}}], "filter": [{"term": {"field_keyword_05": a.country}}]}},
                "size": size, "from": start, "sort": [{"dcdate": "desc"}, {"_score": "desc"}], "lang": "zz",
                "_source": ["title", "url", "description", "dcdate", "field_keyword_05", "field_keyword_08",
                            "field_keyword_17", "field_keyword_19"]},
                {"Origin": "https://www.ibm.com", "Referer": "https://www.ibm.com/careers/search"})["hits"]
            rows = r.get("hits", [])
            for h in rows:
                p = h["_source"]
                loc = re.sub(r",\s*IN$", "", p.get("field_keyword_19") or "").title()
                posted = (p.get("dcdate") or "")[:10]
                merge(jobs, make_job(
                    c["name"], "ibm", p["title"], f"{loc}, {a.country}" if loc else a.country, posted,
                    age_from_iso(posted), p["url"], p["url"].rsplit("=", 1)[-1], term,
                    text=" ".join(x for x in (p.get("description"), p.get("field_keyword_08"),
                                              p.get("field_keyword_17")) if x)))
            start += size
            if not rows or start >= r.get("total", {}).get("value", 0):
                break
    return list(jobs.values())


# --------------------------------------------------------------------------- SAP
SAP_JOB_RE = re.compile(r'\{"id":"(\d+)","title":"((?:[^"\\]|\\.)*)","url":"([^"]+)","score":[\d.]+,'
                        r'"teams":"([^"]*)","locations":"([^"]*)","displayDate":"([^"]+)"')


def fetch_sap(c, a):
    """SAP: jobs.sap.com is server rendered; the job list is embedded in the page as JSON (24 per page).
    It has no keyword search, so every India page is read and matched on title and team."""
    hdr = {"Accept-Language": "en-US,en;q=0.9"}
    found, page = {}, 1
    while True:
        html = http_text(f"https://jobs.sap.com/en/jobs/?country={urllib.parse.quote(a.country)}&page={page}",
                         hdr).replace('\\"', '"')
        new = [m for m in SAP_JOB_RE.findall(html) if m[0] not in found]
        for m in new:
            found[m[0]] = m
        if not new or page >= 50:
            break
        page += 1
    jobs = {}
    for jid, title, url, team, loc, shown in found.values():
        title = json.loads(f'"{title}"')
        for term in kw_hits(f"{title} {team}", a):
            merge(jobs, make_job(c["name"], "sap", title, loc, shown[:10], age_from_iso(shown),
                                 "https://jobs.sap.com" + url, jid, term, text=team))
    return list(jobs.values())


FETCHERS = {"workday": fetch_workday, "oracle": fetch_oracle, "greenhouse": fetch_greenhouse,
            "jibe": fetch_jibe, "sf_rss": fetch_sf_rss, "eightfold": fetch_eightfold,
            "phenom": fetch_phenom, "goldman": fetch_goldman, "radancy": fetch_radancy,
            "icims": fetch_icims, "brassring": fetch_brassring, "deshaw": fetch_deshaw,
            "smartrecruiters": fetch_smartrecruiters, "lever": fetch_lever, "bofa": fetch_bofa,
            "amazon": fetch_amazon, "atlassian": fetch_atlassian, "avature": fetch_avature,
            "cornerstone": fetch_cornerstone, "apple": fetch_apple, "taleo": fetch_taleo, "google": fetch_google, "meta": fetch_meta, "ibm": fetch_ibm, "sap": fetch_sap}


# --------------------------------------------------------------------------- state / output
def load_state(path):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_outputs(out_dir, jobs, manual, sector="", label="jobs"):
    out_dir.mkdir(parents=True, exist_ok=True)
    fields = ["company", "title", "location", "posted", "age_days", "relevance", "matched",
              "platform", "work_mode", "job_id", "url", "first_seen"]
    if any(j.get("sector") for j in jobs):
        fields.insert(0, "sector")
    with open(out_dir / "jobs.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for j in jobs:
            w.writerow({**j, "matched": ", ".join(sorted(j["matched"]))})
    lines = [f"# {sector} - {label} - {date.today()}", "",
             f"{len(jobs)} job(s).", "",
             "| Posted | Company | Role | Location | Match | Keywords |", "|---|---|---|---|---|---|"]
    for j in jobs:
        lines.append(f"| {j['posted']} | {j['company']} | [{j['title']}]({j['url']}) | "
                     f"{j['location']} | {j['relevance']} | {', '.join(sorted(j['matched']))} |")
    if manual:
        lines += ["", "## Not covered by the scraper (check manually)", ""]
        lines += [f"- {m['name']} ({m['platform']}): {m['url']}" for m in manual]
    (out_dir / "jobs.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


ISO_DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def add_skills(jobs, a, state=None):
    """Tag each job with the skill terms found in its title/description; a skill in the title
    makes it a 'title' match. Workday descriptions are fetched here, one request per job, except for
    jobs an earlier run already read (the state keeps their skills, work mode and posting date)."""
    key = ",".join(sorted(x.lower() for x in a.skills))
    cache = {}
    for j in jobs:
        e = (state or {}).get(j["url"])
        if j["detail"] and not j["text"] and e and e.get("kw") == key and "wm" in e:
            cache[j["url"]] = e
    todo = [j for j in jobs if j["detail"] and not j["text"] and j["url"] not in cache]
    if todo:
        print(f"Fetching descriptions for {len(todo)} job(s) (one request each; this can take a few minutes); "
              f"{len(cache)} reused from earlier runs...", file=sys.stderr, flush=True)
    done = [0]

    def load(j):
        if j["detail"] and not j["text"] and j["url"] not in cache:
            try:
                data = http_json(j["detail"], retries=2, timeout=20)
                if "jobPostingInfo" in data:  # Workday
                    info = data["jobPostingInfo"]
                    j["text"] = strip_tags(info.get("jobDescription", ""))
                    if not j["posted"] and info.get("startDate"):  # some tenants hide the date in the list
                        j["posted"], j["age_days"] = info["startDate"], age_from_iso(info["startDate"])
                    if not j["location"] and info.get("location"):
                        j["location"] = info["location"]
                else:  # SmartRecruiters
                    secs = (data.get("jobAd") or {}).get("sections") or {}
                    j["text"] = strip_tags(" ".join(s.get("text", "") for s in secs.values()))
            except Exception as e:  # skills just stay unmatched for this job
                log.debug("description fetch failed for %s: %s", j["url"], e)
            done[0] += 1
            if done[0] % 200 == 0:
                print(f"  descriptions: {done[0]}/{len(todo)}", file=sys.stderr, flush=True)
    with cf.ThreadPoolExecutor(a.workers) as ex:
        list(ex.map(load, jobs))
    for j in jobs:
        e = cache.get(j["url"])
        if e:
            found = set(e.get("sk", []))
            j["work_mode"] = e["wm"]
            if not j["posted"] and ISO_DAY_RE.fullmatch(e.get("posted", "")):
                j["posted"], j["age_days"] = e["posted"], age_from_iso(e["posted"])
            if not j["location"] and e.get("loc"):
                j["location"] = e["loc"]
        else:
            text = j["title"] + " " + j["text"]
            found = {s for s in a.skills if has_term(text, s)}
        if j["detail"]:
            j["_sk"] = found  # kept in the state for the next run
        j["matched"] |= found
        if any(has_term(j["title"], s) for s in found):
            j["relevance"] = "title"


def dump_jobs(path, jobs, n_sites, errors):
    """Shard result for --merge-jobs: jobs without their (large) description text."""
    out = [{**{k: v for k, v in j.items() if k not in ("text", "detail")},
            "matched": sorted(j["matched"]), **({"_sk": sorted(j["_sk"])} if "_sk" in j else {})} for j in jobs]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sites": n_sites, "errors": errors, "jobs": out}, ensure_ascii=False),
                    encoding="utf-8")


def load_shards(folder):
    """Read what dump_jobs wrote; the same job found by two shards is merged."""
    merged, n_sites, errors = {}, 0, {}
    files = sorted(folder.glob("*.json"))
    if not files:
        sys.exit(f"No shard files (*.json) in {folder}")
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        n_sites += d["sites"]
        errors.update(d["errors"])
        for j in d["jobs"]:
            j["matched"] = set(j["matched"])
            if "_sk" in j:
                j["_sk"] = set(j["_sk"])
            j.setdefault("text", "")
            j.setdefault("detail", "")
            merge(merged, j)
    return list(merged.values()), n_sites, errors


def split_terms(a, kind, c, parts=4):
    """Per-term search sites run the keyword list in parallel chunks; others run once."""
    if (kind in ("greenhouse", "deshaw", "brassring", "lever", "bofa", "atlassian") or c.get("delay")
            or len(a.keywords) < 2 * parts):
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
<title>__SECTOR__ - __LABEL__ - __TODAY__</title>
<style>
 body{font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;color:#1f2328;background:#f6f8fa}
 header{padding:16px 24px;background:#0f62fe;color:#fff}
 header h1{margin:0 0 4px;font-size:20px} header p{margin:0;opacity:.9}
 .layout{display:flex;align-items:flex-start}
 .bar{flex:0 0 240px;box-sizing:border-box;display:flex;flex-direction:column;gap:10px;padding:16px;background:#fff;
      border-right:1px solid #d0d7de;position:sticky;top:0;height:100vh;overflow-y:auto}
 .bar input[type=search],.bar select{width:100%;box-sizing:border-box;padding:7px 10px;border:1px solid #d0d7de;border-radius:6px}
 .bar label{display:flex;gap:6px;align-items:center}
 .count{order:-1;font-weight:600;color:#1f2328}
 #reset{padding:7px 10px;border:1px solid #d0d7de;border-radius:6px;background:#eef1f4;cursor:pointer;font:inherit}
 #reset:hover{background:#e1e6eb}
 main{flex:1;min-width:0;padding:16px 24px;overflow-x:auto}
 @media (max-width:800px){.layout{flex-direction:column}.bar{flex:none;width:100%;height:auto;position:static;border-right:0;border-bottom:1px solid #d0d7de}}
 table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #d0d7de}
 th,td{padding:8px 10px;border-bottom:1px solid #eaeef2;text-align:left;vertical-align:top}
 th{background:#eef1f4;cursor:pointer;white-space:nowrap;user-select:none}
 th:hover{background:#e1e6eb} tr:hover td{background:#f0f6ff}
 a{color:#0969da;text-decoration:none;font-weight:600} a:hover{text-decoration:underline}
 .tag{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px}
 .title{background:#dafbe1;color:#116329} .description{background:#fff8c5;color:#7d4e00}
 .mode-remote{background:#ddf4ff;color:#0550ae} .mode-hybrid{background:#f3e8ff;color:#6639ba}
 .new{background:#ffebe9;color:#a40e26;margin-left:6px;font-weight:600}
 .kw{color:#57606a;font-size:12px} .none{padding:24px;text-align:center;color:#57606a}
 h2{font-size:16px;margin:28px 0 8px} ul{margin:0;padding-left:20px} li{margin:3px 0}
</style></head><body>
<header><h1>__SECTOR__: __LABEL__</h1>
<p id="sub"></p></header>
<div class="layout">
<div class="bar">
 <input type="search" id="q" placeholder="Search jobs..." title="Searches role, company, location and keywords">
 <select id="sector" hidden></select>
 <select id="company"></select>
 <select id="city"></select>
 <select id="kwsel"></select>
 <select id="platform"></select>
 <select id="mode"><option value="">Any work mode</option><option value="remote">Remote / WFH</option>
  <option value="hybrid">Hybrid</option><option value="onsite">On-site / not stated</option></select>
 <select id="match"><option value="">All matches</option>
  <option value="title">Keyword in title</option><option value="description">Keyword in description only</option></select>
 <select id="age"><option value="">Any age</option><option value="today" id="todayopt" title="Jobs whose posting date is today (jobs without a posting date are hidden)">Today</option><option value="1">Last 1 day</option>
  <option value="3">Last 3 days</option><option value="7">Last 7 days</option><option value="14">Last 14 days</option></select>
 <button type="button" id="reset" title="Clear the search box and every filter">Reset filters</button>
 <span class="count" id="count"></span>
</div>
<main>
<div id="failed" hidden style="background:#fff8c5;border:1px solid #d4a72c;border-radius:6px;padding:10px 14px;margin-bottom:12px"></div>
<table><thead><tr id="head"></tr></thead><tbody id="body"></tbody></table>
<div class="none" id="none" hidden>No jobs match the filters.</div>
<div id="manual"></div>
</main>
</div>
<script>
const D = __DATA__;
const HAS_SECTOR = D.jobs.some(j => j.sector);
const COLS = [["Posted","age_days"],...(HAS_SECTOR ? [["Sector","sector"]] : []),["Company","company"],["Role","title"],["Location","location"],
              ["Match","relevance"],["Mode","work_mode"],["Keywords","kw"],["Platform","platform"]];
const $ = id => document.getElementById(id);
let sortKey = null, sortDir = 1;
D.jobs.forEach((j,i) => { j.i = i; j.kw = j.matched.join(", "); j.isNew = j.first_seen === D.today; });
$("todayopt").textContent = `Today (${D.jobs.filter(j=>j.age_days === 0).length})`;
$("sub").textContent = `${D.jobs.length} matching jobs, ${D.jobs.filter(j=>j.age_days === 0).length} posted today, ${D.jobs.filter(j=>j.isNew).length} first seen by the scraper today (${D.today}). Generated ${D.generated}.`;
D.jobs.forEach(j => { j.cityList = j.cities.length ? j.cities : ["Other / not stated"]; });
function fill(id, all, values) {
  const sel = $(id);
  [["", all], ...[...new Set(values)].sort().map(v => [v, v])].forEach(([v,t]) => {
    const o = document.createElement("option"); o.value = v; o.textContent = t; sel.append(o); });
}
if (HAS_SECTOR) { $("sector").hidden = false; fill("sector", "All sectors", D.jobs.map(j => j.sector)); }
fill("company", "All companies", D.jobs.map(j => j.company));
fill("city", "All cities", D.jobs.flatMap(j => j.cityList));
fill("kwsel", "All keywords", D.jobs.flatMap(j => j.matched));
fill("platform", "All platforms", D.jobs.map(j => j.platform));
const head = $("head");
COLS.forEach(([label,key]) => { const th = document.createElement("th"); th.textContent = label;
  th.onclick = () => { sortDir = sortKey === key ? -sortDir : 1; sortKey = key; render(); }; head.append(th); });

function cell(tr, text, cls) { const td = document.createElement("td"); td.textContent = text; if (cls) td.className = cls; tr.append(td); return td; }
function render() {
  const q = $("q").value.toLowerCase().trim(), comp = $("company").value, m = $("match").value, sec = $("sector").value,
        city = $("city").value, kw = $("kwsel").value, plat = $("platform").value, mode = $("mode").value,
        ageSel = $("age").value, onlyToday = ageSel === "today", age = onlyToday ? 0 : parseInt(ageSel || "0");
  let rows = D.jobs.filter(j =>
    (!sec || j.sector === sec) && (!comp || j.company === comp) && (!m || j.relevance === m) && (!onlyToday || j.age_days === 0) &&
    (!city || j.cityList.includes(city)) && (!kw || j.matched.includes(kw)) &&
    (!plat || j.platform === plat) && (!mode || (mode === "onsite" ? !j.work_mode : j.work_mode === mode)) &&
    (!age || (j.age_days !== null && j.age_days <= age)) &&
    (!q || (j.title + " " + j.company + " " + j.location + " " + j.cityList.join(" ") + " " + j.kw).toLowerCase().includes(q)));
  if (sortKey) rows.sort((a,b) => {
    let x = a[sortKey], y = b[sortKey];
    if (sortKey === "age_days") { x = x ?? 999; y = y ?? 999; }
    return (x < y ? -1 : x > y ? 1 : a.i - b.i) * sortDir; });
  const body = $("body"); body.innerHTML = "";
  rows.forEach(j => {
    const tr = document.createElement("tr");
    cell(tr, j.posted); if (HAS_SECTOR) cell(tr, (j.sector || "").replace(/_/g, " "), "kw"); cell(tr, j.company);
    const td = document.createElement("td");
    const a = document.createElement("a"); a.textContent = j.title;
    if (/^https?:\/\//.test(j.url)) { a.href = j.url; a.target = "_blank"; a.rel = "noopener noreferrer"; }
    td.append(a);
    if (j.isNew) { const n = document.createElement("span"); n.className = "tag new"; n.textContent = "NEW"; td.append(n); }
    tr.append(td); cell(tr, j.location);
    const mt = cell(tr, ""); const tag = document.createElement("span");
    tag.className = "tag " + j.relevance; tag.textContent = j.relevance; mt.append(tag);
    const wm = cell(tr, ""); if (j.work_mode) { const t = document.createElement("span");
      t.className = "tag mode-" + j.work_mode; t.textContent = j.work_mode === "remote" ? "Remote / WFH" : "Hybrid"; wm.append(t); }
    cell(tr, j.kw, "kw"); cell(tr, j.platform, "kw");
    body.append(tr); });
  $("count").textContent = `${rows.length} of ${D.jobs.length} jobs`;
  $("none").hidden = rows.length > 0;
}
$("reset").addEventListener("click", () => {
  ["q","sector","company","city","kwsel","platform","mode","match","age"].forEach(id => { $(id).value = ""; });
  sortKey = null; sortDir = 1; render(); });
["q","sector","company","city","kwsel","platform","mode","match","age"].forEach(id => $(id).addEventListener(id === "q" ? "input" : "change", render));
if (Object.keys(D.failed).length) {
  const b = $("failed"); b.hidden = false;
  b.textContent = `${Object.keys(D.failed).length} site(s) could not be fetched in this run, so their jobs are missing: ` +
    Object.keys(D.failed).sort().join(", ") + ". HTTP 521/5xx, maintenance redirects or DNS errors mean the site or the network was down; run again later.";
}
if (D.manual.length) {
  const box = $("manual"), h = document.createElement("h2"), ul = document.createElement("ul");
  h.textContent = "Not covered by the scraper - check these career sites manually"; box.append(h, ul);
  D.manual.forEach(m => { const li = document.createElement("li"), a = document.createElement("a");
    a.textContent = m.name; a.href = m.url; a.target = "_blank"; a.rel = "noopener noreferrer";
    li.append(a, " (" + m.platform + (m.sector ? ", " + m.sector.replace(/_/g, " ") : "") + ")"); ul.append(li); });
}
render();
</script></body></html>
"""


def keyword_label(keywords, limit=6):
    """'sre, devops, kubernetes jobs' for report titles; long lists are shortened."""
    shown = ", ".join(keywords[:limit]) + (f" +{len(keywords) - limit} more" if len(keywords) > limit else "")
    return f"{shown} jobs"


def write_html(path, jobs, manual, today, a, errors=None):
    """Self-contained, sortable and filterable report with clickable links."""
    fields = ("sector", "company", "title", "location", "posted", "age_days", "relevance", "platform",
              "work_mode", "url", "first_seen")
    data = {"today": today, "generated": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
            "manual": manual, "failed": errors or {},
            "jobs": [{**{k: j.get(k) for k in fields}, "matched": sorted(j["matched"]),
                      "cities": cities_of(j["location"], a)}
                     for j in jobs]}
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")  # keeps </script> and <!-- out of the data
    html = (HTML_TEMPLATE.replace("__TODAY__", today).replace("__SECTOR__", htmllib.escape(a.sector))
            .replace("__LABEL__", htmllib.escape(keyword_label(a.keywords))).replace("__DATA__", blob))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


# --------------------------------------------------------------------------- main
def main(sector, config, output_dir):
    """Run one sector: `sector` names the report, `config` is its company list (JSON path) and
    `output_dir` is where its reports and seen-jobs state go. Both paths can be overridden by flags."""
    ap = argparse.ArgumentParser(description=f"{sector} jobs scraper (India by default).",
                                 formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--country", default="India", help="location country name (default India)")
    ap.add_argument("--country-code", default="IN", help="ISO code used by Oracle HCM (default IN)")
    ap.add_argument("-k", "--keywords", default="",
                    help="comma separated search terms sent to each career site, e.g. "
                         "\"sre,devops,kubernetes,mlops\" or \"java,testing\". Nothing is built in: "
                         "when omitted in a terminal you are asked for them")
    ap.add_argument("--skills", default="",
                    help="optional comma separated terms matched against each found job's text "
                         "(no extra searches); they are tagged on the job and a skill in the title "
                         "makes it a 'title' match")
    ap.add_argument("--title-words", default="",
                    help="optional comma separated words that make a job a 'title' match when they "
                         "appear in its title (whole word; 'reliab*' = starts with). A job whose "
                         "title contains one of the keywords is always a title match")
    ap.add_argument("--max-age-days", type=int, default=14,
                    help="drop jobs older than this (Workday ages are approximate)")
    ap.add_argument("--title-only", action="store_true",
                    help="keep only jobs whose title contains a keyword, skill or title word")
    ap.add_argument("--remote-only", action="store_true",
                    help="keep only remote / work-from-home jobs (found in the title, location or description)")
    ap.add_argument("--show-all", action="store_true",
                    help="report every match, not only jobs not seen in earlier runs")
    ap.add_argument("--companies", default="", help="comma separated company-name filter")
    ap.add_argument("--config", default=str(config))
    ap.add_argument("--output-dir", default=str(output_dir))
    ap.add_argument("--workers", type=int, default=32,
                    help="parallel requests (lower it if a site starts rejecting requests)")
    ap.add_argument("--max-per-term", type=int, default=200,
                    help="max results fetched per company and keyword")
    ap.add_argument("--retry-failed", type=int, default=1,
                    help="how many times to retry sites that failed, after a pause (default 1; 0 = off)")
    ap.add_argument("--retry-wait", type=int, default=20, help="seconds to wait before a retry (default 20)")
    ap.add_argument("--open", action="store_true", help="open the HTML report in the browser")
    ap.add_argument("--shard", default="", metavar="I/N",
                    help="scan only every N-th site, starting at I (0-based), e.g. 1/4; use with --dump-jobs")
    ap.add_argument("--dump-jobs", default="", metavar="FILE",
                    help="scrape, write the found jobs to FILE (JSON) and stop: no state, no report")
    ap.add_argument("--merge-jobs", default="", metavar="DIR",
                    help="do not scrape: read the *.json files that --dump-jobs wrote into DIR, "
                         "then update the state and write the reports as usual")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    a.sector = sector
    a.keywords = split_list(a.keywords)
    a.skills = split_list(a.skills)
    if not a.keywords and sys.stdin.isatty():
        a.keywords = split_list(input("Keywords to search, comma separated (e.g. sre,devops,kubernetes): "))
    if not a.keywords:
        ap.error("no keywords. Give them with --keywords \"sre,devops,kubernetes\" "
                 "(comma separated; any terms, for example \"java,testing\")")
    set_title_words(split_list(a.title_words))
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")

    cfg = json.loads(Path(a.config).read_text())
    wanted = {n.strip().lower() for n in a.companies.split(",") if n.strip()}
    tasks = [(kind, c) for kind in FETCHERS for c in cfg.get(kind, [])
             if not wanted or any(w in c["name"].lower() for w in wanted)]
    manual = [m for m in cfg.get("manual", [])
              if not wanted or any(w in m["name"].lower() for w in wanted)]
    if a.shard:
        idx, count = (int(x) for x in a.shard.split("/"))
        tasks = [t for i, t in enumerate(sorted(tasks, key=lambda t: (t[0], t[1]["name"]))) if i % count == idx]
    if not tasks and not a.merge_jobs:
        sys.exit("No companies matched.")

    out_root = Path(a.output_dir)
    state_path = out_root / "seen_jobs.json"
    state = load_state(state_path)
    skills_key = ",".join(sorted(x.lower() for x in a.skills))
    sector_of = {c["name"]: c["sector"] for k in FETCHERS for c in cfg.get(k, []) if c.get("sector")}
    if a.merge_jobs:
        jobs, n_sites, errors = load_shards(Path(a.merge_jobs))
        failed = sorted(errors)
    else:
        merged = {}

        def run(batch):
            """Fetch every task of `batch` in parallel; return {company name: first error}."""
            errors = {}
            with cf.ThreadPoolExecutor(a.workers) as ex:
                futs = {ex.submit(FETCHERS[k], c, b): c["name"]
                        for k, c in batch for b in split_terms(a, k, c)}
                for fut in cf.as_completed(futs):
                    name = futs[fut]
                    try:
                        res = fut.result()
                        log.info("%s: %d match(es)", name, len(res))
                        for j in res:
                            merge(merged, j)
                    except Exception as e:
                        errors.setdefault(name, str(e))
            return errors

        errors = run(tasks)
        for attempt in range(a.retry_failed):
            if not errors:
                break
            print(f"{len(errors)} site(s) failed; retrying them in {a.retry_wait}s "
                  f"(retry {attempt + 1}/{a.retry_failed})...", file=sys.stderr)
            time.sleep(a.retry_wait)
            errors = run([(k, c) for k, c in tasks if c["name"] in errors])
        failed = sorted(errors)
        for name, err in sorted(errors.items()):
            print(f"[error] {name}: {err}", file=sys.stderr)
        all_jobs = list(merged.values())

        jobs = [j for j in all_jobs
                if j["age_days"] is None or j["age_days"] <= a.max_age_days]
        add_skills(jobs, a, state)
        jobs = [j for j in jobs if j["age_days"] is None or j["age_days"] <= a.max_age_days]  # dates found late
        for j in jobs:
            j["sector"] = sector_of.get(j["company"], "")
            if "work_mode" not in j:  # cached jobs already carry it
                j["work_mode"] = work_mode(j)
        if a.remote_only:
            jobs = [j for j in jobs if j["work_mode"] == "remote"]
        jobs = [j for j in jobs if not a.title_only or j["relevance"] == "title"]
        n_sites = len(tasks)
        if a.dump_jobs:
            dump_jobs(Path(a.dump_jobs), jobs, n_sites, errors)
            print(f"{n_sites} sites scanned, {len(errors)} failed, {len(jobs)} job(s) written to {a.dump_jobs}.")
            return

    # state: url -> first_seen / last_seen, used to report only new jobs on later runs
    today = date.today().isoformat()
    for j in jobs:
        entry = state.setdefault(j["url"], {"first_seen": today})
        entry["last_seen"] = today
        j["first_seen"] = entry["first_seen"]
        if "_sk" in j:  # what the description gave: later runs reuse it instead of fetching the page again
            entry.update(kw=skills_key, wm=j["work_mode"], sk=sorted(j["_sk"]))
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", j["posted"] or ""):
                entry["posted"] = j["posted"]
            if j["location"]:
                entry["loc"] = j["location"]
    cutoff = (date.today() - timedelta(days=90)).isoformat()
    state = {u: e for u, e in state.items() if e.get("last_seen", today) >= cutoff}
    out_root.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=1))

    report = [j for j in jobs if a.show_all or j["first_seen"] == today]
    report.sort(key=sort_key)
    write_outputs(out_root / today, report, manual, sector, keyword_label(a.keywords))
    # The HTML report lists every matching job and marks the ones first seen today as NEW.
    report_path = out_root / today / "report.html"
    write_html(report_path, sorted(jobs, key=sort_key), manual, today, a, errors)
    (out_root / "latest.html").write_text(report_path.read_text(encoding="utf-8"), encoding="utf-8")

    print(f"{n_sites} sites scanned, {len(failed)} failed, {len(jobs)} matching job(s), "
          f"{len(report)} {'in report' if a.show_all else 'new'}.")
    if report:
        with_sector = bool(sector_of)
        print_table(
            ["#", "Posted"] + (["Sector"] if with_sector else []) + ["Company", "Role", "Location", "Match", "Keywords"],
            [[i, j["posted"]] + ([j["sector"]] if with_sector else []) +
             [j["company"], clip(j["title"], 60), clip(j["location"], 30),
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
        print(f"Failed ({len(failed)}):", ", ".join(sorted(failed)))
        print("These sites returned errors, so their jobs are missing from this report. HTTP 521/5xx or a maintenance "
              "redirect means the site's own servers are down (for example a Workday outage): run again later.")
