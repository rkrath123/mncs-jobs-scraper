"""Offline unit tests for workday_india_jobs.py (no network: HTTP is faked with httpx.MockTransport).

    cd ATS/workday-india-jobs && python3 -m unittest discover -s tests -v

Needs httpx (the script itself needs it); the tests are skipped when it is not installed.
"""
import argparse
import asyncio
import csv
import json
import sys
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    import httpx
    import workday_india_jobs as w
except ModuleNotFoundError as exc:  # pragma: no cover
    raise unittest.SkipTest(f"missing dependency: {exc}")


async def no_sleep(_seconds):
    """Stand-in for asyncio.sleep so pauses and backoff cost no real time."""


def read_csv(path):
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def india_facets():
    return [{"facetParameter": "locationCountry", "descriptor": "Country",
             "values": [{"descriptor": "India", "id": "IN1", "count": 3}]}]


SITE = w.Site("Acme", "https://acme.wd1.myworkdayjobs.com/Careers", "acme", "wd1", "Careers")


def make_scraper(handler, keywords=("devops",), match="description", details=True, max_days=14, rate=0):
    s = w.Scraper(list(keywords), match, 5, 5.0, details, max_days, rate=rate)
    s.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return s


def tenant_handler(jobs, details, facets=None):
    """Fake Workday tenant. `jobs`: {externalPath: (title, postedOn)}; `details`: {externalPath: description html}."""
    def handler(req):
        if req.method == "POST":
            body = json.loads(req.content)
            posts = [{"title": t, "externalPath": p, "locationsText": "Pune, India", "postedOn": posted,
                      "bulletFields": ["R" + p[-1]]} for p, (t, posted) in jobs.items()] if body["searchText"] else []
            return httpx.Response(200, json={"facets": india_facets() if facets is None else facets,
                                             "total": len(posts), "jobPostings": posts})
        path = req.url.path.split("/Careers", 1)[1]
        if path not in details:
            return httpx.Response(404, text="not found")
        return httpx.Response(200, json={"jobPostingInfo": {
            "location": "Pune, India", "startDate": date.today().isoformat(),
            "jobReqId": "R" + path[-1], "jobDescription": details[path]}})
    return handler


class Helpers(unittest.TestCase):
    def test_keyword_regex_is_whole_word(self):
        rx = w.keyword_regex("sre")
        self.assertTrue(rx.search("Senior SRE Engineer"))
        self.assertFalse(rx.search("Sreeram"))
        self.assertFalse(rx.search("misrepresent"))

    def test_keyword_regex_phrase_matches_any_whitespace(self):
        rx = w.keyword_regex("AI engineer")
        self.assertTrue(rx.search("Applied ai   engineer II"))
        self.assertFalse(rx.search("AI engineering lead"))
        self.assertFalse(w.keyword_regex("Ai enginer").search("Applied AI Engineer"))  # a typo never matches

    def test_html_to_text(self):
        self.assertEqual(w.html_to_text("<p>Hello&nbsp;<b>world</b></p>\n<li>x &amp; y</li>"), "Hello world x & y")

    def test_posted_date(self):
        today = date.today()
        self.assertEqual(w.posted_date("2026-01-05"), date(2026, 1, 5))
        self.assertEqual(w.posted_date("Posted Today"), today)
        self.assertEqual(w.posted_date("Posted Yesterday").toordinal(), today.toordinal() - 1)
        self.assertEqual(w.posted_date("Posted 3 Days Ago").toordinal(), today.toordinal() - 3)
        self.assertEqual(w.posted_date("Posted 30+ Days Ago").toordinal(), today.toordinal() - 30)
        self.assertIsNone(w.posted_date(""))
        self.assertIsNone(w.posted_date("whenever"))

    def test_too_old(self):
        self.assertTrue(w.too_old("Posted 30+ Days Ago", 14))
        self.assertFalse(w.too_old("Posted 3 Days Ago", 14))
        self.assertFalse(w.too_old("Posted 30+ Days Ago", 0))  # 0 = no limit
        self.assertFalse(w.too_old("", 14))  # unknown date is kept

    def test_cities_of_merges_aliases(self):
        self.assertEqual(w.cities_of("Bengaluru, India | Gurugram, India"), ["Bangalore", "Gurgaon"])
        self.assertEqual(w.cities_of("Indianapolis, IN"), [])

    def test_retryable_reasons(self):
        for r in ("HTTP 429", "HTTP 503", "ConnectError", "ReadTimeout", "partial: HTTP 429", "partial: ReadError"):
            self.assertTrue(w.retryable(r), r)
        for r in ("HTTP 404", "HTTP 422", "HTTP 401", "invalid JSON", "HTTP 303 -> https://x/maintenance"):
            self.assertFalse(w.retryable(r), r)


class IndiaFilter(unittest.TestCase):
    def test_country_facet_wins(self):
        facets = [{"facetParameter": "locationCountry", "descriptor": "Country", "values": [
            {"descriptor": "United States", "id": "US", "count": 9}, {"descriptor": "India", "id": "IN", "count": 4}]},
            {"facetParameter": "locations", "descriptor": "Locations", "values": [
                {"descriptor": "Pune", "id": "PN", "count": 2}]}]
        self.assertEqual(w.india_filter(facets), ({"locationCountry": ["IN"]}, 4))

    def test_nested_groups_are_searched(self):
        facets = [{"facetParameter": "locationMainGroup", "descriptor": "Location", "values": [
            {"facetParameter": "locationCountry", "descriptor": "Country",
             "values": [{"descriptor": "India", "id": "IN", "count": 7}]}]}]
        self.assertEqual(w.india_filter(facets), ({"locationCountry": ["IN"]}, 7))

    def test_falls_back_to_indian_cities(self):
        facets = [{"facetParameter": "locations", "descriptor": "Locations", "values": [
            {"descriptor": "Hyderabad", "id": "H", "count": 2}, {"descriptor": "Pune", "id": "P", "count": 1},
            {"descriptor": "London", "id": "L", "count": 50}]}]
        applied, total = w.india_filter(facets)
        self.assertEqual(applied, {"locations": ["H", "P"]})
        self.assertEqual(total, 3)

    def test_state_or_site_code_without_city_or_country(self):
        state = [{"facetParameter": "locations", "descriptor": "Locations", "values": [
            {"descriptor": "Telangana (Sandoz)", "id": "T", "count": 49}, {"descriptor": "Basel (Sandoz)", "id": "B", "count": 9}]}]
        self.assertEqual(w.india_filter(state), ({"locations": ["T"]}, 49))
        code = [{"facetParameter": "locations", "descriptor": "Locations", "values": [
            {"descriptor": "IN-BLR-BCPV", "id": "1", "count": 8}, {"descriptor": "IN-HYD-SAR1", "id": "2", "count": 24},
            {"descriptor": "US-CHI-CARP210", "id": "3", "count": 5}, {"descriptor": "Merrillville IN-SLC", "id": "4", "count": 7}]}]
        self.assertEqual(w.india_filter(code), ({"locations": ["1", "2"]}, 32))

    def test_indiana_is_not_india(self):
        facets = [{"facetParameter": "locations", "descriptor": "Locations", "values": [
            {"descriptor": "IN - Indianapolis", "id": "I", "count": 5}, {"descriptor": "Indiana", "id": "J", "count": 5}]}]
        self.assertEqual(w.india_filter(facets), ({}, 0))

    def test_no_facets(self):
        self.assertEqual(w.india_filter([]), ({}, 0))


class SitesCsv(unittest.TestCase):
    def test_load_sites_parses_dedupes_and_skips_bad_urls(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sites.csv"
            p.write_text("name,slug,url\n"
                         "Acme,acme/x,https://acme.wd1.myworkdayjobs.com/Careers\n"
                         "Acme again,acme/x,https://ACME.wd1.myworkdayjobs.com/careers\n"       # same site, other case
                         "Loc,loc/x,https://loc.wd5.myworkdayjobs.com/en-US/External\n"          # locale prefix
                         "Bad,bad,https://example.com/jobs\n"
                         ",noname,https://noname.wd3.myworkdayjobs.com/Site\n", encoding="utf-8")
            sites = w.load_sites(str(p))
        self.assertEqual([(s.name, s.tenant, s.instance, s.site) for s in sites],
                         [("Acme", "acme", "wd1", "Careers"), ("Loc", "loc", "wd5", "External"),
                          ("noname", "noname", "wd3", "Site")])
        self.assertEqual(sites[0].api, "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Careers")

    def test_ensure_local_csv_downloads_only_when_needed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "workday.csv"
            resp = mock.Mock(content=b"name,slug,url\n")
            resp.raise_for_status.return_value = resp
            with mock.patch.object(w, "CSV_PATH", path), mock.patch.object(w.httpx, "get", return_value=resp) as get:
                self.assertEqual(w.ensure_local_csv(), path)
                self.assertEqual(get.call_count, 1)          # missing -> downloaded
                self.assertTrue(path.exists())
                w.ensure_local_csv()
                self.assertEqual(get.call_count, 1)          # present -> not downloaded again
                w.ensure_local_csv(refresh=True)
                self.assertEqual(get.call_count, 2)          # --refresh-csv -> downloaded again


class RequestAndRateLimit(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = mock.patch.object(w.asyncio, "sleep", no_sleep)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clients = []

    async def asyncTearDown(self):
        for s in self.clients:
            await s.close()

    def scraper(self, handler, **kw):
        s = make_scraper(handler, **kw)
        self.clients.append(s)
        return s

    async def test_client_sends_the_headers_that_avoid_workday_429s(self):
        real = w.Scraper(["devops"], "description", 5, 5.0, True)   # the script's own client, not a test double
        self.clients.append(real)
        self.assertEqual(real.client.headers["content-type"], "application/json")
        self.assertEqual(real.client.headers["accept-encoding"], "identity")

    async def test_success(self):
        s = self.scraper(lambda req: httpx.Response(200, json={"ok": 1}))
        resp, reason = await s._request("GET", "https://x.test/a")
        self.assertEqual((resp.json(), reason), ({"ok": 1}, ""))

    async def test_404_is_not_retried(self):
        calls = []
        s = self.scraper(lambda req: calls.append(1) or httpx.Response(404))
        resp, reason = await s._request("GET", "https://x.test/a")
        self.assertEqual((resp, reason, len(calls)), (None, "HTTP 404", 1))

    async def test_server_error_is_retried_then_reported(self):
        calls = []
        s = self.scraper(lambda req: calls.append(1) or httpx.Response(503))
        resp, reason = await s._request("GET", "https://x.test/a")
        self.assertEqual((resp, reason, len(calls)), (None, "HTTP 503", w.MAX_RETRIES))

    async def test_429_then_success_pauses_everything(self):
        calls = []

        def handler(req):
            calls.append(1)
            return httpx.Response(429) if len(calls) < 3 else httpx.Response(200, json={})
        s = self.scraper(handler, rate=10)
        resp, _ = await s._request("GET", "https://x.test/a")
        self.assertIsNotNone(resp)
        self.assertEqual(len(calls), 3)
        self.assertGreater(s._gate_until, 0)      # a global pause was set
        self.assertGreater(s.interval, 0.1)       # and the request rate was reduced

    async def test_429_forever_gives_up_with_reason(self):
        calls = []
        s = self.scraper(lambda req: calls.append(1) or httpx.Response(429))
        resp, reason = await s._request("GET", "https://x.test/a")
        self.assertEqual((resp, reason, len(calls)), (None, "HTTP 429", w.MAX_429_WAITS))

    async def test_retry_after_header_sets_the_pause(self):
        s = self.scraper(lambda req: httpx.Response(200), rate=0)
        loop = asyncio.get_running_loop()
        s._backoff("45")
        self.assertGreaterEqual(s._gate_until - loop.time(), 44)

    async def test_backoff_escalates_once_per_pause(self):
        s = self.scraper(lambda req: httpx.Response(200), rate=10)
        s._backoff("")
        first_interval, first_penalty = s.interval, s._penalty
        s._backoff("")   # another in-flight request answered 429 during the same pause: no second escalation
        self.assertEqual((s.interval, s._penalty), (first_interval, first_penalty))
        self.assertEqual(first_penalty, 10.0)
        self.assertAlmostEqual(first_interval, 0.2)   # 10 req/s halved to 5 req/s

    async def test_rate_halves_but_never_below_one_request_per_3s(self):
        s = self.scraper(lambda req: httpx.Response(200), rate=10)
        for _ in range(12):
            s._gate_until = 0.0   # pretend each pause ended before the next 429
            s._backoff("")
        self.assertEqual(s.interval, 3.0)
        self.assertEqual(s._penalty, 120.0)

    async def test_rate_recovers_after_clean_requests_but_not_above_ceiling(self):
        s = self.scraper(lambda req: httpx.Response(200, json={}), rate=10)
        s._backoff("")
        slowed = s.interval
        for _ in range(30):
            await s._request("GET", "https://x.test/a")
        self.assertLess(s.interval, slowed)
        for _ in range(600):
            await s._request("GET", "https://x.test/a")
        self.assertAlmostEqual(s.interval, 0.1)       # back at the configured ceiling, not faster

    async def test_pace_spreads_requests_over_time(self):
        slept = []

        async def record(seconds):
            slept.append(seconds)
        s = self.scraper(lambda req: httpx.Response(200), rate=2)   # one request per 0.5s
        with mock.patch.object(w.asyncio, "sleep", record):
            for _ in range(3):
                await s._pace()
        self.assertEqual(len(slept), 2)
        self.assertLess(slept[0], slept[1])
        self.assertGreater(slept[1], 0.5)


class DetailCacheTests(unittest.TestCase):
    def test_roundtrip_and_expiry(self):
        c = w.DetailCache(":memory:", ttl_days=1)
        self.assertIsNone(c.get("u"))
        c.put("u", {"description": "héllo"})
        self.assertEqual(c.get("u"), {"description": "héllo"})
        with mock.patch.object(w.time, "time", return_value=w.time.time() + 2 * 86400):
            self.assertIsNone(c.get("u"))                           # older than the ttl
        c.close()

    def test_persists_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "sub" / "c.sqlite"
            c = w.DetailCache(path)
            c.put("u", {"a": 1})
            c.close()
            c2 = w.DetailCache(path)
            self.assertEqual(c2.get("u"), {"a": 1})
            c2.close()

    def test_site_filter_roundtrip_expiry_and_off_switch(self):
        c = w.DetailCache(":memory:", site_ttl_days=1)
        self.assertIsNone(c.get_site("api"))
        c.put_site("api", {"locationCountry": ["IN"]})
        c.put_site("none", {})                                       # "no India jobs" is remembered too
        self.assertEqual(c.get_site("api"), {"locationCountry": ["IN"]})
        self.assertEqual(c.get_site("none"), {})
        with mock.patch.object(w.time, "time", return_value=w.time.time() + 2 * 86400):
            self.assertIsNone(c.get_site("api"))                     # older than the ttl
        c.close()
        off = w.DetailCache(":memory:", site_ttl_days=0)
        off.put_site("api", {"x": ["1"]})
        self.assertIsNone(off.get_site("api"))
        off.close()


class ScrapeSite(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher = mock.patch.object(w.asyncio, "sleep", no_sleep)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.scrapers = []

    async def asyncTearDown(self):
        for s in self.scrapers:
            await s.close()

    def scraper(self, handler, **kw):
        s = make_scraper(handler, **kw)
        self.scrapers.append(s)
        return s

    async def test_title_and_description_matches(self):
        handler = tenant_handler(
            jobs={"/job/a1": ("Senior DevOps Engineer", "Posted Today"),
                  "/job/b2": ("Platform Engineer", "Posted 2 Days Ago"),
                  "/job/c3": ("Accountant", "Posted 2 Days Ago")},
            details={"/job/a1": "<p>x</p>", "/job/b2": "<p>You will run DevOps pipelines.</p>",
                     "/job/c3": "<p>Bookkeeping only.</p>"})
        s = self.scraper(handler)
        jobs = {j.title: j for j in await s.scrape_site(SITE)}
        self.assertEqual(set(jobs), {"Senior DevOps Engineer", "Platform Engineer"})
        self.assertTrue(jobs["Senior DevOps Engineer"].in_title)
        self.assertFalse(jobs["Platform Engineer"].in_title)          # keyword only in the description
        self.assertEqual(jobs["Platform Engineer"].keywords, {"devops"})
        self.assertIn("run DevOps pipelines", jobs["Platform Engineer"].description)
        self.assertEqual(s.failed, [])
        self.assertEqual(s.stats["jobs"], 2)

    async def test_cached_details_are_reused_without_a_detail_request(self):
        detail_calls = []
        base = tenant_handler(jobs={"/job/a1": ("Platform Engineer", "Posted Today")},
                              details={"/job/a1": "<p>We run DevOps.</p>"})

        def handler(req):
            if req.method == "GET":
                detail_calls.append(req.url.path)
            return base(req)
        cache = w.DetailCache(":memory:")
        first = self.scraper(handler)
        first.cache = cache
        second = self.scraper(handler)
        second.cache = cache
        j1 = await first.scrape_site(SITE)
        j2 = await second.scrape_site(SITE)
        self.assertEqual(len(detail_calls), 1)                      # the second run did not fetch the posting again
        self.assertEqual(second.stats["cache_hits"], 1)
        self.assertEqual(second.stats["req_detail"], 0)
        self.assertEqual([(j.title, j.description, j.location, j.req_id) for j in j1],
                         [(j.title, j.description, j.location, j.req_id) for j in j2])
        cache.close()

    async def test_title_mode_skips_description_matches(self):
        handler = tenant_handler(
            jobs={"/job/a1": ("DevOps Engineer", "Posted Today"), "/job/b2": ("Platform Engineer", "Posted Today")},
            details={"/job/a1": "<p>x</p>", "/job/b2": "<p>devops</p>"})
        jobs = await self.scraper(handler, match="title").scrape_site(SITE)
        self.assertEqual([j.title for j in jobs], ["DevOps Engineer"])

    async def test_old_postings_are_dropped_before_any_detail_request(self):
        detail_calls = []
        base = tenant_handler(jobs={"/job/a1": ("DevOps Engineer", "Posted 30+ Days Ago")}, details={"/job/a1": "x"})

        def handler(req):
            if req.method == "GET":
                detail_calls.append(req.url.path)
            return base(req)
        self.assertEqual(await self.scraper(handler).scrape_site(SITE), [])
        self.assertEqual(detail_calls, [])

    async def test_cached_site_filter_skips_the_probe(self):
        probes = []
        base = tenant_handler(jobs={"/job/a1": ("DevOps Engineer", "Posted Today")}, details={"/job/a1": "x"})

        def handler(req):
            if req.method == "POST" and json.loads(req.content)["searchText"] == "":
                probes.append(req.url.path)
            return base(req)
        cache = w.DetailCache(":memory:")
        first = self.scraper(handler)
        first.cache = cache
        second = self.scraper(handler)
        second.cache = cache
        j1 = await first.scrape_site(SITE)
        j2 = await second.scrape_site(SITE)
        self.assertEqual(len(probes), 1)                            # the second run reused the stored India filter
        self.assertEqual(second.stats["site_cache_hits"], 1)
        self.assertEqual([j.title for j in j1], [j.title for j in j2])
        cache.close()

    async def test_site_without_india_is_remembered_and_dead_site_is_not(self):
        calls = []
        no_india = tenant_handler(jobs={}, details={}, facets=[])
        cache = w.DetailCache(":memory:")

        def handler(req):
            calls.append(req.url.path)
            return no_india(req)
        for _ in range(2):
            s = self.scraper(handler)
            s.cache = cache
            self.assertEqual(await s.scrape_site(SITE), [])
        self.assertEqual(len(calls), 1)                             # "no India jobs" was cached after the first probe
        dead = self.scraper(lambda req: httpx.Response(422))
        dead.cache = cache
        other = w.Site("Dead", "https://d.wd1.myworkdayjobs.com/X", "d", "wd1", "X")
        await dead.scrape_site(other)
        self.assertIsNone(cache.get_site(other.api))                # a failed probe is never cached
        cache.close()

    async def test_site_without_india_jobs_is_skipped_after_the_probe(self):
        searched = []
        base = tenant_handler(jobs={}, details={}, facets=[])

        def handler(req):
            searched.append(json.loads(req.content)["searchText"])
            return base(req)
        s = self.scraper(handler)
        self.assertEqual(await s.scrape_site(SITE), [])
        self.assertEqual(searched, [""])              # only the probe, no keyword searches
        self.assertEqual(s.stats["no_india"], 1)

    async def test_dead_site_is_reported_with_reason(self):
        s = self.scraper(lambda req: httpx.Response(422))
        self.assertEqual(await s.scrape_site(SITE), [])
        self.assertEqual(s.failed, [(SITE, "HTTP 422")])
        self.assertEqual(s.stats["dead"], 1)

    async def test_failed_description_request_marks_site_partial_instead_of_silently_dropping(self):
        handler = tenant_handler(jobs={"/job/b2": ("Platform Engineer", "Posted Today")}, details={})  # detail -> 404
        s = self.scraper(handler)
        self.assertEqual(await s.scrape_site(SITE), [])
        self.assertEqual(s.failed, [(SITE, w.PARTIAL + "HTTP 404")])

    async def test_failed_later_page_marks_site_partial_but_keeps_what_was_found(self):
        def handler(req):
            if req.method == "POST":
                body = json.loads(req.content)
                if body["offset"] >= w.PAGE_LIMIT:
                    return httpx.Response(404)
                posts = [{"title": "DevOps Engineer", "externalPath": "/job/a1", "locationsText": "Pune, India",
                          "postedOn": "Posted Today", "bulletFields": ["R1"]}] if body["searchText"] else []
                return httpx.Response(200, json={"facets": india_facets(), "total": 45, "jobPostings": posts})
            return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "x"}})
        s = self.scraper(handler)
        jobs = await s.scrape_site(SITE)
        self.assertEqual([j.title for j in jobs], ["DevOps Engineer"])
        self.assertEqual(s.failed, [(SITE, w.PARTIAL + "HTTP 404")])


class RunAndOutput(unittest.IsolatedAsyncioTestCase):
    def args(self, csv_path, **kw):
        base = dict(keywords="devops", csv=str(csv_path), company=None, max_sites=None, refresh_csv=False,
                    match="description", concurrency=5, timeout=5.0, no_details=False, days=14, workers=3,
                    rate=0, retry_passes=2, retry_wait=0, no_cache=True, cache_days=14, site_cache_days=7)
        base.update(kw)
        return argparse.Namespace(**base)

    def write_csv(self, d, n):
        p = Path(d) / "sites.csv"
        p.write_text("name,slug,url\n" + "".join(
            f"T{i},t{i},https://t{i}.wd1.myworkdayjobs.com/Careers\n" for i in range(n)), encoding="utf-8")
        return p

    async def test_failed_sites_are_retried_and_recovered(self):
        fails_left = {"t1": 4}   # t1: 503 more often than a single request retries -> fails pass 1, works in pass 2
        base = tenant_handler(jobs={"/job/a1": ("DevOps Engineer", "Posted Today")}, details={"/job/a1": "x"})

        def handler(req):
            host = req.url.host.split(".")[0]
            if fails_left.get(host, 0) > 0:
                fails_left[host] -= 1
                return httpx.Response(503)
            return base(req)
        real_client = httpx.AsyncClient

        def client(**kw):
            kw["transport"] = httpx.MockTransport(handler)
            return real_client(**kw)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(w.asyncio, "sleep", no_sleep), \
                mock.patch.object(w.httpx, "AsyncClient", client):
            jobs, failed = await w.run(self.args(self.write_csv(d, 3)))
        self.assertEqual(failed, [])
        self.assertEqual(sorted(j.company for j in jobs), ["T0", "T1", "T2"])

    async def test_non_retryable_failures_stay_in_the_failed_list(self):
        real_client = httpx.AsyncClient
        attempts = []

        def handler(req):
            attempts.append(req.url.host)
            return httpx.Response(404)

        def client(**kw):
            kw["transport"] = httpx.MockTransport(handler)
            return real_client(**kw)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(w.asyncio, "sleep", no_sleep), \
                mock.patch.object(w.httpx, "AsyncClient", client):
            jobs, failed = await w.run(self.args(self.write_csv(d, 2)))
        self.assertEqual(jobs, [])
        self.assertEqual([(s.name, r) for s, r in failed], [("T0", "HTTP 404"), ("T1", "HTTP 404")])
        self.assertEqual(len(attempts), 2)            # not retried

    async def test_company_and_max_sites_filters(self):
        real_client = httpx.AsyncClient
        probed = []

        def handler(req):
            probed.append(req.url.host.split(".")[0])
            return httpx.Response(200, json={"facets": [], "total": 0, "jobPostings": []})

        def client(**kw):
            kw["transport"] = httpx.MockTransport(handler)
            return real_client(**kw)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(w.httpx, "AsyncClient", client):
            csv_path = self.write_csv(d, 5)
            await w.run(self.args(csv_path, company="t3"))
            self.assertEqual(probed, ["t3"])
            probed.clear()
            await w.run(self.args(csv_path, max_sites=2))
            self.assertEqual(sorted(probed), ["t0", "t1"])

    def test_same_posting_on_two_sites_is_deduplicated(self):
        # run() keys on (host, external_path); two Job objects for one posting merge their keywords
        a = w.Job("A", "s1", "DevOps", "Pune", "Posted Today", "R1", "https://h.wd1.myworkdayjobs.com/s1/job/x",
                  keywords={"devops"}, external_path="/job/x")
        b = w.Job("A", "s2", "DevOps", "Pune", "Posted Today", "R1", "https://h.wd1.myworkdayjobs.com/s2/job/x",
                  keywords={"sre"}, external_path="/job/x")
        unique = {}
        for j in (a, b):
            key = (w.urlsplit(j.url).hostname or "", j.external_path)
            if key in unique:
                unique[key].keywords |= j.keywords
            else:
                unique[key] = j
        self.assertEqual(len(unique), 1)
        self.assertEqual(next(iter(unique.values())).keywords, {"devops", "sre"})


class Outputs(unittest.TestCase):
    def job(self):
        return w.Job("Acme", "Careers", "DevOps Engineer", "Pune, India", date.today().isoformat(), "R1",
                     "https://acme.wd1.myworkdayjobs.com/Careers/job/x", keywords={"devops"}, in_title=True,
                     external_path="/job/x", description="devops pipelines")

    def test_outputs_go_into_a_date_folder(self):
        with tempfile.TemporaryDirectory() as d:
            html_path = w.write_outputs([self.job()], [(SITE, "HTTP 429")], d, ["devops"])
            folder = Path(d) / datetime.now().strftime("%Y-%m-%d")
            self.assertEqual(html_path.parent, folder)
            self.assertEqual(html_path.suffix, ".html")
            names = sorted(p.name.split("_20")[0] for p in folder.iterdir())
            self.assertEqual(names, ["workday_failed_sites", "workday_india_jobs", "workday_india_jobs",
                                     "workday_india_jobs"])
            rows = read_csv(next(folder.glob("workday_india_jobs_*.csv")))
            self.assertEqual((rows[0]["company"], rows[0]["keywords"]), ("Acme", "devops"))
            failed = read_csv(next(folder.glob("workday_failed_sites_*.csv")))
            self.assertEqual(failed[0]["reason"], "HTTP 429")
            self.assertIn("DevOps Engineer", html_path.read_text(encoding="utf-8"))

    def test_html_escapes_script_end_tag(self):
        j = self.job()
        j.title = "</script><b>x"
        page = w.render_html([{"company": "A", "title": j.title, "location": "", "posted": "", "req_id": "",
                               "time_type": "", "url": "", "keywords": ["k"], "in_title": True}], ["k"])
        self.assertNotIn("</script><b>", page)


class MainCli(unittest.TestCase):
    def run_main(self, *argv):
        async def fake_run(args):
            return [w.Job("Acme", "Careers", "DevOps Engineer", "Pune, India", "", "R1", "https://x/y",
                          keywords={"devops"}, external_path="/job/x")], []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(w, "run", fake_run), \
                mock.patch.object(w.webbrowser, "open") as opened, \
                mock.patch.object(sys, "argv", ["workday_india_jobs.py", "-k", "devops", "-o", d, *argv]):
            w.main()
            return opened, d

    def test_open_flag_opens_the_report_in_the_browser(self):
        with mock.patch.object(w, "write_outputs", wraps=w.write_outputs) as write:
            opened, _ = self.run_main("--open")
        opened.assert_called_once()
        url = opened.call_args[0][0]
        self.assertTrue(url.startswith("file://") and url.endswith(".html"), url)
        self.assertIn(datetime.now().strftime("%Y-%m-%d"), url)
        write.assert_called_once()

    def test_without_open_flag_the_browser_is_not_started(self):
        opened, _ = self.run_main()
        opened.assert_not_called()

    def test_short_keyword_option_and_new_options_parse(self):
        captured = {}

        async def fake_run(args):
            captured.update(vars(args))
            return [], []
        with tempfile.TemporaryDirectory() as d, mock.patch.object(w, "run", fake_run), \
                mock.patch.object(sys, "argv", ["x", "-k", "a,b", "-o", d, "--rate", "3", "--retry-passes", "1",
                                                "--retry-wait", "5"]):
            w.main()
        self.assertEqual((captured["keywords"], captured["rate"], captured["retry_passes"], captured["retry_wait"]),
                         ("a,b", 3.0, 1, 5))
        self.assertIsNone(captured["csv"])           # default: the local workday.csv


if __name__ == "__main__":
    unittest.main()
