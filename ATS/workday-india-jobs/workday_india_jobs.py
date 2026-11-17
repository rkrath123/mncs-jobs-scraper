#!/usr/bin/env python3
"""Scrape Workday career sites for India based jobs that match keywords.

Source of sites: https://github.com/kalil0321/ats-scrapers/blob/main/ats-companies/workday.csv
(columns: name,slug,url - url looks like https://{tenant}.wdN.myworkdayjobs.com/{site}).

How it works, per career site:
  1. One probe request (no search text) returns the site's facets. From them we find the
     India filter: the "country" facet value "India", or - when the site has no country
     facet - every "locations" facet value that names India or an Indian city.
  2. For every keyword, one search (searchText=<keyword> + the India filter) is paginated.
     Sites with no India jobs are dropped after step 1, so most sites cost a single request.
  3. Hits are checked against the keywords (see --match), then the posting detail is
     fetched for the matches to get the full location list, posting date and description.

Keywords are an argument, never hard coded in the logic:
    python3 workday_india_jobs.py --keywords "sre,devops,kubernetes,terraform"

Requires: pip install httpx
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import html
import io
import json
import re
import sqlite3
import sys
import time
import webbrowser
import zlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx

CSV_URL = "https://raw.githubusercontent.com/kalil0321/ats-scrapers/main/ats-companies/workday.csv"
CSV_PATH = Path(__file__).resolve().parent / "workday.csv"  # local copy of CSV_URL, downloaded once
# posting details of earlier runs. Outside the project on purpose: SQLite fails ("disk I/O error") on cloud-synced folders
CACHE_PATH = Path.home() / ".cache" / "mncs-jobs-scraper" / "workday-details.sqlite"
# comma separates keywords, "|" separates alternative spellings of one keyword (the first one is its label)
DEFAULT_KEYWORDS = "sre|site reliability,devops|dev ops|devsecops,kubernetes|k8s"
PAGE_LIMIT = 20  # Workday rejects larger values with HTTP 400.
RESULT_CAP = 2000  # Workday reports at most 2000 hits per query.
RETRY_STATUSES = {403, 429, 502, 503, 504}
MAX_RETRIES = 3
PARTIAL = "partial: "  # prefix of a failure reason when only some requests of a site failed
MAX_429_WAITS = 12  # a rate-limited request waits and retries this many times before it gives up
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

URL_RE = re.compile(r"^https://(?P<tenant>[^.]+)\.(?P<instance>wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[^/?#]+)")

INDIA_CITIES = [
    "India", "Bangalore", "Bengaluru", "Bangaluru", "Bengalooru", "Banglore", "Hyderabad", "Pune",
    "Chennai", "Mumbai", "Navi Mumbai", "Thane", "Gurgaon", "Gurugram", "Grugram", "Gurgoan", "Noida", "Greater Noida", "Delhi", "New Delhi", "Faridabad",
    "Ghaziabad", "Manesar", "Kolkata", "Calcutta", "Ahmedabad", "Gandhinagar", "Vadodara",
    "Surat", "Kochi", "Cochin", "Trivandrum", "Thiruvananthapuram", "Coimbatore", "Madurai",
    "Mysore", "Mysuru", "Mangalore", "Mangaluru", "Hubli", "Belgaum", "Jaipur", "Chandigarh",
    "Mohali", "Panchkula", "Indore", "Bhopal", "Nagpur", "Nashik", "Pimpri", "Chinchwad",
    "Lucknow", "Kanpur", "Bhubaneswar", "Bhubaneshwar", "Visakhapatnam", "Vizag", "Vijayawada",
    "Guwahati", "Patna", "Ranchi", "Dehradun", "Goa", "Pondicherry", "Puducherry", "Sriperumbudur",
    "Oragadam", "Tirupati", "Hosur", "Whitefield", "Electronic City",
]
INDIA_LOCATION_RE = re.compile(
    r"(?<![A-Za-z])(?:" + "|".join(re.escape(c) for c in INDIA_CITIES) + r")(?![A-Za-z])"
    r"|^(?-i:IND)(?:[\s\-_,:]|$)",  # not bare "IN": that is Indiana ("IN - Indianapolis")
    re.IGNORECASE,
)
# facet parameter or label that says "this facet lists places" (some tenants name the parameter "a", "b", ...)
INDIA_WORD_RE = re.compile(r"(?<![A-Za-z])India(?![A-Za-z])", re.IGNORECASE)
NON_PLACE_FACET_RE = re.compile(r"remote|worker|time ?type|skill|schedule|shift", re.IGNORECASE)
PLACE_FACET_RE = re.compile(r"locat|countr|region|state|city|office|site|geograph|primary|campus", re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------- models
@dataclass
class Site:
    name: str
    url: str
    tenant: str
    instance: str
    site: str

    @property
    def base(self) -> str:
        return f"https://{self.tenant}.{self.instance}.myworkdayjobs.com"

    @property
    def api(self) -> str:
        return f"{self.base}/wday/cxs/{self.tenant}/{self.site}"


@dataclass
class Job:
    company: str
    site: str
    title: str
    location: str
    posted: str
    req_id: str
    url: str
    keywords: set[str] = field(default_factory=set)
    in_title: bool = False
    time_type: str = ""
    remote_type: str = ""
    external_path: str = ""
    description: str = ""


# --------------------------------------------------------------------------- helpers
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def ensure_local_csv(refresh: bool = False) -> Path:
    """The default sites list is the local workday.csv next to this script; download it when missing."""
    if refresh or not CSV_PATH.exists():
        log(f"downloading {CSV_URL} -> {CSV_PATH}")
        CSV_PATH.write_bytes(httpx.get(CSV_URL, follow_redirects=True, timeout=60).raise_for_status().content)
    return CSV_PATH


def load_sites(source: str) -> list[Site]:
    """Read the Workday CSV from a local path or an http(s) URL."""
    if source.startswith(("http://", "https://")):
        text = httpx.get(source, follow_redirects=True, timeout=60).raise_for_status().text
    else:
        text = Path(source).expanduser().read_text(encoding="utf-8")
    sites: list[Site] = []
    seen: set[str] = set()
    for row in csv.DictReader(io.StringIO(text)):
        url = (row.get("url") or "").strip()
        m = URL_RE.match(url)
        if not m:
            continue
        key = f"{m['tenant']}.{m['instance']}/{m['site']}".lower()
        if key in seen:
            continue
        seen.add(key)
        sites.append(Site(row.get("name", "").strip() or m["tenant"], url, m["tenant"], m["instance"], m["site"]))
    return sites


def keyword_regex(keyword: str) -> re.Pattern[str]:
    # whole-word match so "sre" does not hit "Sreeram"; spaces in a keyword match any whitespace
    parts = [re.escape(p) for p in keyword.split()]
    return re.compile(r"(?<![A-Za-z0-9])" + r"\s+".join(parts) + r"(?![A-Za-z0-9])", re.IGNORECASE)


REL_POSTED_RE = re.compile(r"(?:posted\s+)?(today|yesterday|(\d+)\+?\s+days?\s+ago)", re.IGNORECASE)


def posted_date(posted: str) -> date | None:
    """Absolute date of a posting: 'YYYY-MM-DD' (job detail) or 'Posted 3 Days Ago' (listing)."""
    posted = (posted or "").strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", posted):
        try:
            return date.fromisoformat(posted[:10])
        except ValueError:
            return None
    m = REL_POSTED_RE.search(posted)
    if not m:
        return None
    word = m.group(1).lower()
    days = 0 if word == "today" else 1 if word == "yesterday" else int(m.group(2))
    return date.today() - timedelta(days=days)


def too_old(posted: str, max_days: int) -> bool:
    """True when the posting date is known and older than max_days (0 = no limit)."""
    d = posted_date(posted)
    return bool(max_days) and d is not None and (date.today() - d).days > max_days


def html_to_text(value: str) -> str:
    text = html.unescape(value)
    text = TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def walk_facets(facets: list[dict], parent: str = ""):
    """Yield (facetParameter, facetDescriptor, value) for every leaf value, descending into groups."""
    for facet in facets or []:
        if not isinstance(facet, dict):
            continue
        param = facet.get("facetParameter") or parent
        for value in facet.get("values") or []:
            if not isinstance(value, dict):
                continue
            if "facetParameter" in value or "values" in value:
                yield from walk_facets([value], param)
            elif value.get("id"):
                yield param, facet.get("descriptor") or "", value


def india_filter(facets: list[dict]) -> tuple[dict[str, list[str]], int]:
    """Build the appliedFacets that restrict a query to India.

    Returns (applied_facets, india_job_count). Empty dict means the site has no India jobs
    (or exposes no usable location facet).
    """
    country: dict[str, list[tuple[str, int]]] = {}
    location: dict[str, list[tuple[str, int]]] = {}
    other: dict[str, list[tuple[str, int]]] = {}  # last resort, e.g. "Owens & Minor India Private Limited"
    for param, label, value in walk_facets(facets):
        desc = str(value.get("descriptor") or "").strip()
        count = int(value.get("count") or 0)
        key = f"{param} {label}"
        if desc.lower() in ("india", "republic of india") or (
                "country" in key.lower() and desc.lower() in ("in", "ind")):
            country.setdefault(param, []).append((value["id"], count))
        elif PLACE_FACET_RE.search(key) and "remote" not in key.lower() and INDIA_LOCATION_RE.search(desc):
            location.setdefault(param, []).append((value["id"], count))
        elif INDIA_WORD_RE.search(desc) and not NON_PLACE_FACET_RE.search(key):
            other.setdefault(param, []).append((value["id"], count))
    chosen = country or location or other  # country facet is exact; cities, then any "India" value, as fallbacks
    if not chosen:
        return {}, 0
    applied = {p: [i for i, _ in vals] for p, vals in chosen.items()}
    # ids under different params are alternatives; use the biggest count as the estimate
    total = max(sum(c for _, c in vals) for vals in chosen.values())
    return (applied, total) if total > 0 else ({}, 0)


# --------------------------------------------------------------------------- detail cache
class DetailCache:
    """On-disk cache of fetched posting details (location, date, req id, description), keyed by posting URL.
    Most requests of a run are these per-job fetches and postings stay online for weeks, so a repeat run only
    fetches postings it has not seen before. Entries older than `ttl_days` are ignored."""

    def __init__(self, path: Path | str, ttl_days: int = 14):
        self.ttl = ttl_days * 86400
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self._pending = 0
        self.db.execute("CREATE TABLE IF NOT EXISTS detail (url TEXT PRIMARY KEY, fetched REAL, data BLOB)")

    def get(self, url: str) -> dict | None:
        row = self.db.execute("SELECT fetched, data FROM detail WHERE url=?", (url,)).fetchone()
        if not row or time.time() - row[0] > self.ttl:
            return None
        try:
            return json.loads(zlib.decompress(row[1]))
        except (zlib.error, ValueError):
            return None

    def put(self, url: str, data: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO detail VALUES (?, ?, ?)",
                        (url, time.time(), zlib.compress(json.dumps(data).encode("utf-8"))))
        self._pending += 1
        if self._pending >= 200:  # save regularly: an interrupted run keeps what it fetched
            self.db.commit()
            self._pending = 0

    def close(self) -> None:
        self.db.execute("DELETE FROM detail WHERE fetched < ?", (time.time() - self.ttl,))
        self.db.commit()
        self.db.close()


# --------------------------------------------------------------------------- scraper
class Scraper:
    def __init__(self, keywords: list[str], match: str, concurrency: int, timeout: float, details: bool,
                 max_days: int = 0, rate: float = 0, cache: DetailCache | None = None):
        self.cache = cache
        self.keywords = keywords
        self.max_days = max_days
        groups = [[a.strip() for a in k.split("|") if a.strip()] for k in keywords]
        self.queries = [(g[0], alt) for g in groups if g for alt in g]  # (label, search text)
        self.patterns = {g[0]: re.compile("|".join(keyword_regex(a).pattern for a in g), re.IGNORECASE)
                         for g in groups if g}
        self.match = match
        self.details = details
        self.sem = asyncio.Semaphore(concurrency)
        self.client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,  # dead tenants redirect to a maintenance page
            # Header set matters: in A/B tests against Workday, requests with httpx's default headers (or with only one of
            # Content-Type / Accept-Encoding set) got HTTP 429 on ~50% of requests even at 3 requests/s, while this exact
            # set (the same one urllib sends: explicit Content-Type, no compression) was never limited.
            headers={"User-Agent": UA, "Accept": "application/json", "Content-Type": "application/json",
                     "Accept-Encoding": "identity"},
            limits=httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency),
        )
        # Workday (behind Cloudflare) rate-limits per IP across ALL tenants, so pacing is shared by every request:
        # `rate` = max requests started per second (0 = unlimited); a 429 pauses everybody (_backoff).
        self.base_interval = self.interval = 1.0 / rate if rate > 0 else 0.0  # `rate` is the ceiling, see _backoff
        self._ok = 0
        self._next_slot = 0.0
        self._gate_until = 0.0
        self._penalty = 0.0
        self.failed: list[tuple[Site, str]] = []
        self.stats = {"sites": 0, "dead": 0, "no_india": 0, "with_india": 0, "errors": 0, "jobs": 0,
                      "req_probe": 0, "req_search": 0, "req_detail": 0, "cache_hits": 0, "http_429": 0}

    async def close(self) -> None:
        await self.client.aclose()

    async def _pace(self) -> None:
        """Wait for our slot: spreads requests over time and holds them back while a rate-limit pause is active."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        slot = max(self._next_slot, now, self._gate_until)
        self._next_slot = slot + self.interval
        if slot > now:
            await asyncio.sleep(slot - now)

    def _backoff(self, retry_after: str) -> None:
        """A 429 arrived: pause every request. The pause doubles (10s .. 120s) while 429s keep coming."""
        now = asyncio.get_running_loop().time()
        if now >= self._gate_until:  # a 429 from a request sent after the last pause ended: escalate
            self._penalty = min(max(self._penalty * 2, 10.0), 120.0)
            # slow down too (halve the rate, down to one request per 3s); speeds back up in _request()
            self.interval = min(max(self.interval * 2, 0.1), 3.0)
            self._ok = 0
        wait = max(float(retry_after) if retry_after.isdigit() else 0.0, self._penalty)
        if now + wait > self._gate_until + 1:  # one message per pause, not one per in-flight request
            log(f"  rate limited (HTTP 429): pausing all requests for {wait:.0f}s, "
                f"then at most {1 / self.interval:.1f} requests/s")
        self._gate_until = max(self._gate_until, now + wait)

    async def _request(self, method: str, url: str, **kw) -> tuple[httpx.Response | None, str]:
        """HTTP with retries. Returns (response, "") or (None, reason) when the site is unusable."""
        reason = "no response"
        attempt = waits_429 = 0
        while attempt < MAX_RETRIES:
            await self._pace()
            async with self.sem:
                try:
                    resp = await self.client.request(method, url, **kw)
                except httpx.HTTPError as exc:
                    resp = None
                    reason = type(exc).__name__
            if resp is not None:
                if resp.status_code == 200:
                    self._penalty = self._penalty * 0.9 if self._penalty > 1 else 0.0  # recover slowly
                    self._ok += 1
                    if self._ok >= 30 and self.interval > self.base_interval:  # 30 clean requests: speed up 25%
                        self._ok = 0
                        self.interval = max(self.interval * 0.75, self.base_interval)
                    return resp, ""
                reason = f"HTTP {resp.status_code}"
                if resp.status_code in (301, 302, 303, 307, 308):
                    reason += " -> " + resp.headers.get("location", "")
                if resp.status_code == 429:
                    self.stats["http_429"] += 1
                    self._backoff(resp.headers.get("Retry-After", ""))
                    waits_429 += 1
                    if waits_429 >= MAX_429_WAITS:
                        return None, reason
                    continue  # the pause happens in _pace(); a 429 does not use up a normal retry
                if resp.status_code not in RETRY_STATUSES:
                    return None, reason
                retry_after = resp.headers.get("Retry-After", "")
                await asyncio.sleep(min(30.0, float(retry_after) if retry_after.isdigit() else 2.0**attempt))
            else:
                await asyncio.sleep(2.0**attempt)
            attempt += 1
        return None, reason

    async def _search(self, site: Site, applied: dict, text: str, offset: int, limit: int) -> tuple[dict | None, str]:
        body = {"appliedFacets": applied, "limit": limit, "offset": offset, "searchText": text}
        self.stats["req_probe" if text == "" and limit == 1 else "req_search"] += 1
        resp, reason = await self._request("POST", f"{site.api}/jobs", json=body)
        if resp is None:
            return None, reason
        try:
            data = resp.json()
        except ValueError:
            return None, "invalid JSON"
        return (data, "") if isinstance(data, dict) else (None, "unexpected JSON")

    async def _search_all(self, site: Site, applied: dict, text: str) -> tuple[list[dict] | None, str]:
        """All postings of one query, or (None, reason); a failed later page is reported as a reason too."""
        first, reason = await self._search(site, applied, text, 0, PAGE_LIMIT)
        if first is None:
            return None, reason
        postings = list(first.get("jobPostings") or [])
        total = min(int(first.get("total") or 0), RESULT_CAP)
        offsets = range(PAGE_LIMIT, total, PAGE_LIMIT)
        pages = await asyncio.gather(*(self._search(site, applied, text, o, PAGE_LIMIT) for o in offsets))
        for page, why in pages:
            if page is None:
                reason = reason or why
            postings.extend((page or {}).get("jobPostings") or [])
        return postings, reason

    def _title_hits(self, title: str) -> set[str]:
        return {k for k, p in self.patterns.items() if p.search(title)}

    async def _enrich(self, site: Site, job: Job) -> str:
        """Fetch the posting detail into `job`. Returns "" on success, else the failure reason."""
        cached = self.cache.get(job.url) if self.cache else None
        if cached is not None:
            self.stats["cache_hits"] += 1
            job.location = cached["location"] or job.location
            job.posted = cached["posted"] or job.posted
            job.req_id = cached["req_id"] or job.req_id
            job.description = cached["description"]
            return ""
        self.stats["req_detail"] += 1
        resp, reason = await self._request("GET", f"{site.api}{job.external_path}")
        if resp is None:
            return reason
        try:
            info = (resp.json() or {}).get("jobPostingInfo") or {}
        except ValueError:
            return "invalid JSON"
        locs = [v.strip() for v in [info.get("location"), *(info.get("additionalLocations") or [])]
                if isinstance(v, str) and v.strip()]
        if locs:
            job.location = " | ".join(dict.fromkeys(locs))
        if info.get("startDate"):
            job.posted = str(info["startDate"])
        if info.get("jobReqId"):
            job.req_id = str(info["jobReqId"])
        if info.get("jobDescription"):
            job.description = html_to_text(info["jobDescription"])
        if self.cache and info:
            self.cache.put(job.url, {"location": " | ".join(dict.fromkeys(locs)), "posted": str(info.get("startDate") or ""),
                                     "req_id": str(info.get("jobReqId") or ""), "description": job.description})
        return ""

    async def scrape_site(self, site: Site) -> list[Job]:
        self.stats["sites"] += 1
        probe, reason = await self._search(site, {}, "", 0, 1)
        if probe is None:
            self.stats["dead"] += 1
            self.failed.append((site, reason))
            return []
        applied, india_count = india_filter(probe.get("facets") or [])
        if not applied:
            self.stats["no_india"] += 1
            return []
        self.stats["with_india"] += 1

        found: dict[str, Job] = {}
        partial = ""  # first failure that may have hidden jobs of this site; the site is then retried
        for label, kw in self.queries:
            postings, why = await self._search_all(site, applied, kw)
            partial = partial or why
            if postings is None:
                self.stats["errors"] += 1
                continue
            for p in postings:
                path = p.get("externalPath") or ""
                if not path:
                    continue
                job = found.get(path)
                if job is None:
                    bullets = p.get("bulletFields") or []
                    job = found[path] = Job(
                        company=site.name,
                        site=site.site,
                        title=p.get("title") or "",
                        location=p.get("locationsText") or "",
                        posted=p.get("postedOn") or "",
                        req_id=str(bullets[0]) if bullets else "",
                        url=f"{site.base}/{site.site}{path}",
                        time_type=p.get("timeType") or "",
                        remote_type=p.get("remoteType") or "",
                        external_path=path,
                    )
                job.keywords.add(label)

        # recency filter on the listing's relative date ("Posted 30+ Days Ago"), before any detail request
        found = {path: j for path, j in found.items() if not too_old(j.posted, self.max_days)}

        # keyword filter
        kept: list[Job] = []
        if self.match == "any":  # trust Workday's own full-text search
            kept = list(found.values())
            for job in kept:
                job.in_title = bool(self._title_hits(job.title))
        else:
            need_description: list[Job] = []
            for job in found.values():
                hits = self._title_hits(job.title)
                if hits:
                    job.keywords = hits
                    job.in_title = True
                    kept.append(job)
                elif self.match == "description":
                    need_description.append(job)
            if need_description:
                whys = await asyncio.gather(*(self._enrich(site, j) for j in need_description))
                partial = partial or next((w for w in whys if w), "")
                for job in need_description:
                    hits = {k for k, p in self.patterns.items() if p.search(job.description)}
                    if hits:
                        job.keywords = hits
                        kept.append(job)

        if self.details and kept:
            await asyncio.gather(*(self._enrich(site, j) for j in kept if not j.description))
        kept = [j for j in kept if not too_old(j.posted, self.max_days)]  # exact date from the detail
        if partial:  # keep what was found, but list the site as failed so it is retried / reported
            self.failed.append((site, f"{PARTIAL}{partial}"))
        self.stats["jobs"] += len(kept)
        return kept


def retryable(reason: str) -> bool:
    """True for failures worth another attempt: rate limit, server errors, timeouts / connection errors."""
    r = reason.removeprefix(PARTIAL)
    return r.startswith(("HTTP 429", "HTTP 5")) or r.endswith(("Timeout", "Error")) and not r.startswith("HTTP")


async def run(args: argparse.Namespace) -> tuple[list[Job], list[tuple[Site, str]]]:
    keywords = [k.strip() for k in args.keywords.split(",") if k.strip()]
    if not keywords:
        sys.exit("no keywords given")

    sites = load_sites(args.csv or str(ensure_local_csv(args.refresh_csv)))
    if args.company:
        needles = [c.strip().lower() for c in args.company.split(",") if c.strip()]
        sites = [s for s in sites if any(n in s.name.lower() or n in s.tenant.lower() for n in needles)]
    if args.max_sites:
        sites = sites[: args.max_sites]
    log(f"{len(sites)} Workday sites | keywords={keywords} | match={args.match} | last {args.days or 'all'} days")

    cache = None if args.no_cache else DetailCache(CACHE_PATH, ttl_days=args.cache_days)
    scraper = Scraper(keywords, args.match, args.concurrency, args.timeout, not args.no_details, args.days,
                      rate=args.rate, cache=cache)
    results: list[Job] = []
    started = time.monotonic()

    async def process(batch: list[Site], label: str = "") -> None:
        """Scrape `batch` with args.workers parallel workers, collecting jobs into `results`."""
        done = 0
        queue: asyncio.Queue[Site] = asyncio.Queue()
        for s in batch:
            queue.put_nowait(s)

        async def worker() -> None:
            nonlocal done
            while True:
                try:
                    site = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    jobs = await scraper.scrape_site(site)
                except Exception as exc:  # one broken tenant must not stop the run
                    scraper.stats["errors"] += 1
                    log(f"  ! {site.url}: {exc!r}")
                    jobs = []
                for j in jobs:
                    results.append(j)
                    log(f"  + {j.company} | {j.title} | {j.location}")
                done += 1
                if done % 100 == 0 or done == len(batch):
                    st = scraper.stats
                    log(f"{label}[{done}/{len(batch)}] {time.monotonic() - started:.0f}s  "
                        f"india_sites={st['with_india']} failed={len(scraper.failed)} matches={st['jobs']}")

        await asyncio.gather(*(worker() for _ in range(args.workers)))

    try:
        await process(sites)
        for n in range(1, args.retry_passes + 1):
            retry = [(s, r) for s, r in scraper.failed if retryable(r)]
            if not retry:
                break
            log(f"retry pass {n}/{args.retry_passes}: {len(retry)} site(s) failed with a temporary error "
                f"(rate limit / timeout); trying them again in {args.retry_wait}s")
            await asyncio.sleep(args.retry_wait)
            scraper.failed = [(s, r) for s, r in scraper.failed if not retryable(r)]
            scraper.stats["sites"] -= len(retry)
            scraper.stats["dead"] -= sum(not r.startswith(PARTIAL) for _, r in retry)
            await process([s for s, _ in retry], f"retry {n} ")
    finally:
        await scraper.close()
        if cache:
            cache.close()
    st = scraper.stats
    log(f"requests: {st['req_probe']} probes, {st['req_search']} searches, {st['req_detail']} job details "
        f"({st['cache_hits']} more from cache), {st['http_429']} HTTP 429 | {time.monotonic() - started:.0f}s")

    # the same posting is often listed on several sites of one tenant: keep one copy
    unique: dict[tuple[str, str], Job] = {}
    for j in results:
        key = (urlsplit(j.url).hostname or "", j.external_path)
        if key in unique:
            unique[key].keywords |= j.keywords
        else:
            unique[key] = j
    return sorted(unique.values(), key=lambda j: (j.company.lower(), j.title.lower())), scraper.failed


CITY_ALIASES = {"bengaluru": "Bangalore", "bangaluru": "Bangalore", "bengalooru": "Bangalore",
                "banglore": "Bangalore", "gurugram": "Gurgaon", "grugram": "Gurgaon", "gurgoan": "Gurgaon", "cochin": "Kochi", "calcutta": "Kolkata",
                "mysuru": "Mysore", "mangaluru": "Mangalore", "trivandrum": "Thiruvananthapuram",
                "bhubaneshwar": "Bhubaneswar", "vizag": "Visakhapatnam", "puducherry": "Pondicherry"}
CITY_RE = re.compile(
    r"(?<![A-Za-z])(" + "|".join(re.escape(c) for c in sorted(INDIA_CITIES, key=len, reverse=True) if c != "India")
    + r")(?![A-Za-z])", re.IGNORECASE)


def cities_of(location: str) -> list[str]:
    """Canonical Indian city names mentioned in a location string (for the city filter)."""
    found = []
    for m in CITY_RE.finditer(location):
        name = CITY_ALIASES.get(m.group(1).lower(), m.group(1).title())
        if name not in found:
            found.append(name)
    return found


HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Workday India jobs</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f4f6fb;color:#172033;font:14px/1.5 system-ui,Segoe UI,Arial}
header{background:linear-gradient(120deg,#13213d,#2457d6);color:#fff;padding:24px clamp(16px,4vw,46px)}
h1{margin:0 0 4px;font-size:clamp(22px,3vw,32px)}header p{margin:2px 0;color:#dce6ff}
main{padding:18px clamp(12px,3vw,38px) 48px}
.toolbar,.tablewrap{background:#fff;border:1px solid #e2e7f0;border-radius:11px}
.toolbar{padding:12px;display:flex;flex-wrap:wrap;gap:9px;margin-bottom:10px;align-items:center}
input[type=search],select{font:inherit;border:1px solid #cbd3df;border-radius:8px;padding:9px;min-width:170px;background:#fff}
input[type=search]{flex:1;min-width:240px}
.chips{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.chip{border:1px solid #cbd3df;border-radius:99px;padding:4px 11px;cursor:pointer;user-select:none;background:#fff}
.chip.on{background:#2457d6;border-color:#2457d6;color:#fff}
button{border:0;border-radius:8px;background:#647084;color:#fff;padding:9px 14px;font:inherit;cursor:pointer}
#count{margin:6px 2px 10px;color:#48546a}
.tablewrap{overflow:auto}table{border-collapse:collapse;width:100%;min-width:950px}
th,td{padding:9px 12px;border-bottom:1px solid #e7eaf0;text-align:left;vertical-align:top;max-width:420px}
th{position:sticky;top:0;background:#eef2f8;white-space:nowrap;cursor:pointer}th.s-asc:after{content:" \\25B2"}th.s-desc:after{content:" \\25BC"}
tr:hover td{background:#f8faff}a{color:#2457d6;font-weight:650;text-decoration:none}a:hover{text-decoration:underline}
.kw{display:inline-block;background:#e8efff;color:#2457d6;border-radius:5px;padding:0 6px;margin:0 3px 2px 0;font-size:12px}
</style></head><body>
<header><h1>Workday India jobs</h1><p>Generated __GENERATED__ &middot; search keywords: __KEYWORDS__</p></header>
<main>
<div class="toolbar">
 <input type="search" id="q" placeholder="Filter by title, company, location, req id (space = AND)">
 <select id="company"><option value="">All companies</option></select>
 <select id="city"><option value="">All cities</option></select>
 <select id="type"><option value="">Any time type</option></select>
 <select id="age"><option value="">At any time</option><option value="0">Today</option><option value="1">Last 1 day</option>
  <option value="2">Last 2 days</option><option value="3">Last 3 days</option><option value="7">Last 7 days</option>
  <option value="15">Last 15 days</option></select>
 <div class="chips" id="kwchips"></div>
 <label class="chip" style="display:inline-flex;gap:6px"><input type="checkbox" id="intitle"> keyword in title only</label>
 <button id="reset">Reset</button>
</div>
<div id="count"></div>
<div class="tablewrap"><table><thead><tr id="head"></tr></thead><tbody id="rows"></tbody></table></div>
</main>
<script>
const JOBS = __DATA__;
const COLS = [["company","Company"],["title","Title"],["location","Location"],["date","Posted"],["keywords","Keywords"],["time_type","Type"],["req_id","Req id"]];
const $ = id => document.getElementById(id);
const esc = t => String(t ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
let sortKey = "company", sortDir = 1, activeKw = new Set();
const uniq = f => [...new Set(JOBS.flatMap(f).filter(Boolean))].sort((a,b)=>a.localeCompare(b));
function fill(id, vals){ $(id).insertAdjacentHTML("beforeend", vals.map(v=>`<option>${esc(v)}</option>`).join("")); }
fill("company", uniq(j=>[j.company])); fill("city", uniq(j=>j.cities)); fill("type", uniq(j=>[j.time_type]));
const allKw = uniq(j=>j.keywords);
$("kwchips").innerHTML = allKw.map(k=>`<span class="chip" data-k="${esc(k)}">${esc(k)}</span>`).join("");
$("kwchips").onclick = e => { const k = e.target.dataset.k; if(!k) return;
  activeKw.has(k) ? activeKw.delete(k) : activeKw.add(k); e.target.classList.toggle("on"); render(); };
$("head").innerHTML = COLS.map(([k,l])=>`<th data-k="${k}">${l}</th>`).join("");
$("head").onclick = e => { const k = e.target.dataset.k; if(!k) return;
  sortDir = sortKey===k ? -sortDir : 1; sortKey = k; render(); };
["q","company","city","type","age","intitle"].forEach(id => $(id).addEventListener(id==="q"?"input":"change", render));
$("reset").onclick = () => { $("q").value=""; ["company","city","type","age"].forEach(i=>$(i).value="");
  $("intitle").checked=false; activeKw.clear(); document.querySelectorAll(".chip.on").forEach(c=>c.classList.remove("on")); render(); };
function render(){
  const terms = $("q").value.toLowerCase().split(/\\s+/).filter(Boolean);
  const co=$("company").value, ci=$("city").value, ty=$("type").value, ag=$("age").value;
  const rows = JOBS.filter(j =>
    (!co || j.company===co) && (!ci || j.cities.includes(ci)) && (!ty || j.time_type===ty) && (!$("intitle").checked || j.in_title) && (ag==="" || (j._age!==null && j._age<=+ag)) &&
    (!activeKw.size || j.keywords.some(k=>activeKw.has(k))) &&
    terms.every(t => j._h.includes(t)));
  const val = j => Array.isArray(j[sortKey]) ? j[sortKey].join(",") : String(j[sortKey] ?? "");
  rows.sort((a,b)=>val(a).localeCompare(val(b), undefined, {numeric:true, sensitivity:"base"})*sortDir);
  $("rows").innerHTML = rows.map(j=>`<tr><td>${esc(j.company)}</td><td><a href="${esc(j.url)}" target="_blank" rel="noopener">${esc(j.title)}</a></td>`+
    `<td>${esc(j.location)}</td><td>${esc(j.date)}${j._age===null?"":` <span style="color:#647084">(${j._age===0?"today":j._age+"d ago"})</span>`}</td><td>${j.keywords.map(k=>`<span class="kw">${esc(k)}</span>`).join("")}</td>`+
    `<td>${esc(j.time_type)}</td><td>${esc(j.req_id)}</td></tr>`).join("");
  $("count").textContent = `${rows.length} of ${JOBS.length} jobs`;
  document.querySelectorAll("#head th").forEach(th => th.className = th.dataset.k===sortKey ? (sortDir>0?"s-asc":"s-desc") : "");
}
const today = new Date(); today.setHours(0,0,0,0);
JOBS.forEach(j => j._age = j.date ? Math.max(0, Math.round((today - new Date(j.date+"T00:00:00"))/864e5)) : null);
JOBS.forEach(j => j._h = [j.company,j.title,j.location,j.req_id,j.keywords.join(" ")].join(" ").toLowerCase());
render();
</script></body></html>
"""


def render_html(jobs: list[dict], keywords: list[str]) -> str:
    """Self-contained HTML page (data embedded) with text, company, city, type and keyword filters."""
    rows = [{**{c: j.get(c, "") for c in ("company", "title", "location", "posted", "req_id", "time_type", "url")},
             "in_title": bool(j.get("in_title")),
             "keywords": sorted(j.get("keywords") or []),
             "cities": cities_of(j.get("location", "")),
             "date": str(posted_date(j.get("posted", "")) or "")} for j in jobs]
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    return (HTML_TEMPLATE.replace("__DATA__", data)
            .replace("__GENERATED__", datetime.now().strftime("%Y-%m-%d %H:%M"))
            .replace("__KEYWORDS__", html.escape(", ".join(keywords))))


def write_outputs(jobs: list[Job], failed: list[tuple[Site, str]], out: str, keywords: list[str]) -> Path:
    now = datetime.now()
    stamp = now.strftime("%Y%m%d_%H%M%S")
    base = Path(out).expanduser() / now.strftime("%Y-%m-%d")  # one folder per day: output/2026-11-16/
    base.mkdir(parents=True, exist_ok=True)
    csv_path = base / f"workday_india_jobs_{stamp}.csv"
    json_path = base / f"workday_india_jobs_{stamp}.json"
    cols = ["company", "title", "location", "posted", "keywords", "req_id", "time_type", "remote_type", "url"]
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for j in jobs:
            w.writerow([j.company, j.title, j.location, j.posted, ",".join(sorted(j.keywords)),
                        j.req_id, j.time_type, j.remote_type, j.url])
    rows = [{**{c: getattr(j, c) for c in ("company", "site", "title", "location", "posted", "req_id",
                                            "time_type", "remote_type", "url", "description", "in_title")},
             "keywords": sorted(j.keywords)} for j in jobs]
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, ensure_ascii=False)
    html_path = base / f"workday_india_jobs_{stamp}.html"
    html_path.write_text(render_html(rows, keywords), encoding="utf-8")
    failed_path = base / f"workday_failed_sites_{stamp}.csv"
    with failed_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "url", "reason"])
        w.writerows((s.name, s.url, r) for s, r in failed)
    log(f"\n{len(jobs)} jobs for keywords {keywords}\n  {html_path}\n  {csv_path}\n  {json_path}"
        f"\n  {failed_path} ({len(failed)} sites unreachable: retired site or Workday maintenance - rerun later)")
    return html_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-k", "--keywords", default=DEFAULT_KEYWORDS,
                    help=f"comma separated keywords (default: {DEFAULT_KEYWORDS})")
    ap.add_argument("-m", "--match", choices=["title", "description", "any"], default="description",
                    help="title: keyword must be a whole word in the job title; "
                         "description: title OR job description (default; extra request per non-title hit); "
                         "any: keep everything Workday's own search returns (noisy)")
    ap.add_argument("--csv", help="Workday sites CSV: URL or local path (default: workday.csv next to this "
                                  "script, downloaded on first use)")
    ap.add_argument("--refresh-csv", action="store_true", help="download workday.csv again before running")
    ap.add_argument("-o", "--out", default="output", help="output directory (default: ./output)")
    ap.add_argument("--company", help="only sites whose name/tenant contains one of these (comma separated)")
    ap.add_argument("--max-sites", type=int, help="only process the first N sites (for testing)")
    ap.add_argument("--workers", type=int, default=25, help="sites processed in parallel (default 25)")
    ap.add_argument("--concurrency", type=int, default=25, help="max in-flight HTTP requests (default 25)")
    ap.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout seconds (default 30)")
    ap.add_argument("--rate", type=float, default=20.0,
                    help="max requests started per second across all sites (default 20, 0 = unlimited). "
                         "Workday rate-limits per IP: too fast and most sites answer HTTP 429; the script "
                         "slows itself down on a 429, so this is a ceiling")
    ap.add_argument("--no-cache", action="store_true",
                    help="do not read or write the posting-detail cache (~/.cache/mncs-jobs-scraper/workday-details.sqlite)")
    ap.add_argument("--cache-days", type=int, default=14,
                    help="reuse cached posting details up to N days old (default 14)")
    ap.add_argument("--retry-passes", type=int, default=2,
                    help="how many times to retry sites that failed with a temporary error (default 2, 0 = off)")
    ap.add_argument("--retry-wait", type=int, default=60, help="seconds to wait before a retry pass (default 60)")
    ap.add_argument("--no-details", action="store_true",
                    help="skip the per-job detail request (location may stay as '2 Locations', no description)")
    ap.add_argument("-d", "--days", type=int, default=14,
                    help="only jobs posted in the last N days (default 14, 0 = no limit)")
    ap.add_argument("--open", action="store_true", help="open the HTML page in the browser when done")
    ap.add_argument("--from-json", metavar="FILE",
                    help="skip scraping: rebuild the HTML page from an earlier workday_india_jobs_*.json")
    args = ap.parse_args()

    if args.from_json:
        rows = json.loads(Path(args.from_json).expanduser().read_text(encoding="utf-8"))
        rows = [r for r in rows if not too_old(r.get("posted", ""), args.days)]
        kws = sorted({k for r in rows for k in r.get("keywords", [])})
        out = Path(args.from_json).expanduser().with_suffix(".html")
        out.write_text(render_html(rows, kws), encoding="utf-8")
        log(f"{len(rows)} jobs -> {out}")
        if args.open:
            webbrowser.open(out.resolve().as_uri())
        return

    jobs, failed = asyncio.run(run(args))
    html_path = write_outputs(jobs, failed, args.out, [k.strip() for k in args.keywords.split(",") if k.strip()])
    for j in jobs:
        print(f"{j.company} | {j.title} | {j.location} | {j.url}")
    if args.open:
        webbrowser.open(html_path.resolve().as_uri())


if __name__ == "__main__":
    main()
