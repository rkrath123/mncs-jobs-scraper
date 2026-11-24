"""Offline unit tests (no network). Run from the project folder:

    python3 -m unittest discover -s tests -v
"""
import csv
import io
import json
import sys
import tempfile
import unittest
import urllib.error
import urllib.parse
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import jobs_engine as je  # noqa: E402


def args(**kw):
    base = dict(country="India", country_code="IN", keywords=["devops"], skills=["Docker", "Helm"],
                workers=2, max_per_term=40)
    base.update(kw)
    return Namespace(**base)


class TextHelpers(unittest.TestCase):
    def test_age_from_text(self):
        self.assertEqual(je.age_from_text("Posted Today"), 0)
        self.assertEqual(je.age_from_text("Posted Yesterday"), 1)
        self.assertEqual(je.age_from_text("Posted 3 Days Ago"), 3)
        self.assertEqual(je.age_from_text("Posted 30+ Days Ago"), 30)
        self.assertIsNone(je.age_from_text(""))
        self.assertIsNone(je.age_from_text(None))

    def test_age_from_iso(self):
        self.assertEqual(je.age_from_iso((date.today() - timedelta(days=5)).isoformat()), 5)
        self.assertEqual(je.age_from_iso(date.today().isoformat() + "T10:00:00Z"), 0)
        self.assertIsNone(je.age_from_iso("garbage"))
        self.assertIsNone(je.age_from_iso(None))

    def test_has_term_is_whole_word(self):
        self.assertTrue(je.has_term("We use Helm and Docker", "Helm"))
        self.assertFalse(je.has_term("an overwhelming workload", "Helm"))
        self.assertTrue(je.has_term("CI/CD pipelines", "CI/CD"))
        self.assertFalse(je.has_term(None, "Helm"))

    def test_strip_tags(self):
        self.assertEqual(je.strip_tags("<p>Hello&nbsp;<b>World</b></p>\n x"), "Hello World x")
        self.assertEqual(je.strip_tags(None), "")

    def test_clip(self):
        self.assertEqual(je.clip("abc", 5), "abc")
        self.assertEqual(je.clip("abcdef", 4), "abc…")


class Locations(unittest.TestCase):
    def test_loc_ok(self):
        a = args()
        self.assertTrue(je.loc_ok("Pune, Maharashtra", a))
        self.assertTrue(je.loc_ok("Remote - India", a))
        self.assertFalse(je.loc_ok("Indianapolis, IN, United States", a))
        self.assertFalse(je.loc_ok("Indiana, USA", a))
        self.assertFalse(je.loc_ok("London, UK", a))
        self.assertFalse(je.loc_ok("", a))

    def test_cities_of_normalises_aliases(self):
        a = args()
        self.assertEqual(je.cities_of("Bangalore, Karnataka", a), ["Bengaluru"])
        self.assertEqual(je.cities_of("Gurgaon / Noida", a), ["Gurugram", "Noida"])
        self.assertEqual(je.cities_of("London", a), [])
        self.assertEqual(je.cities_of("Pune", args(country="Germany")), [])


class Keywords(unittest.TestCase):
    def tearDown(self):
        je.set_title_words([])

    def test_nothing_is_built_in(self):
        for name in ("DEFAULT_KEYWORDS", "DEFAULT_SKILLS"):
            self.assertFalse(hasattr(je, name), name)
        self.assertIsNone(je.TITLE_RE)

    def test_split_list(self):
        self.assertEqual(je.split_list("sre, devops,,CI/CD ,"), ["sre", "devops", "CI/CD"])
        self.assertEqual(je.split_list(""), [])
        self.assertEqual(je.split_list(None), [])

    def test_title_words_whole_word_and_prefix(self):
        je.set_title_words(["sdet", "test*", "qa"])
        hit = lambda t: bool(je.TITLE_RE.search(t))  # noqa: E731
        self.assertTrue(hit("Senior SDET"))
        self.assertTrue(hit("Testing Lead"))
        self.assertTrue(hit("QA Engineer"))
        self.assertFalse(hit("Squad Lead"))       # 'qa' must be a whole word
        self.assertFalse(hit("Attestation Analyst"))  # prefix only at the start of a word
        je.set_title_words([])
        self.assertIsNone(je.TITLE_RE)

    def test_title_relevance_follows_keywords_and_title_words_only(self):
        mk = lambda title, term: je.make_job("A", "w", title, "Pune", "", 0, "u", 1, term)["relevance"]  # noqa: E731
        self.assertEqual(mk("Java Developer", "java"), "title")
        self.assertEqual(mk("Quality Lead", "java"), "description")
        je.set_title_words(["quality"])
        self.assertEqual(mk("Quality Lead", "java"), "title")


class WorkMode(unittest.TestCase):
    def mode(self, title="Engineer", location="Pune", text=""):
        return je.work_mode({"title": title, "location": location, "text": text})

    def test_remote_from_title_or_location(self):
        self.assertEqual(self.mode(location="Remote - India"), "remote")
        self.assertEqual(self.mode(title="Remote Java Developer"), "remote")
        self.assertEqual(self.mode(title="SRE (WFH)"), "remote")
        self.assertEqual(self.mode(location="Work from home, India"), "remote")

    def test_remote_from_explicit_description_phrases_only(self):
        self.assertEqual(self.mode(text="This is a fully remote position."), "remote")
        self.assertEqual(self.mode(text="You can work from home"), "remote")
        # partial work from home is hybrid, not remote (BlackRock boilerplate)
        self.assertEqual(self.mode(text="at least 4 days in the office per week, with the flexibility to "
                                        "work from home 1 day a week"), "hybrid")
        self.assertEqual(self.mode(text="You can work from home two days a week"), "hybrid")
        # ordinary use of the word must not flag an on-site job
        self.assertEqual(self.mode(text="Manage remote servers and work with remote teams"), "")

    def test_hybrid_and_onsite(self):
        self.assertEqual(self.mode(location="Pune (Hybrid)"), "hybrid")
        self.assertEqual(self.mode(text="Hybrid working model"), "hybrid")
        self.assertEqual(self.mode(), "")
        self.assertEqual(self.mode(location="Remote", text="hybrid"), "remote")  # remote wins


class JobRecords(unittest.TestCase):
    def setUp(self):
        je.set_title_words(["devops", "sre", "site reliab*", "platform eng*"])

    def tearDown(self):
        je.set_title_words([])

    def mk(self, title, url="https://x/1", term="devops"):
        return je.make_job("Acme", "workday", title, "Pune", "Today", 0, url, 7, term)

    def test_relevance(self):
        self.assertEqual(self.mk("Senior DevOps Engineer")["relevance"], "title")
        self.assertEqual(self.mk("Site Reliability Engineer")["relevance"], "title")
        self.assertEqual(self.mk("Platform Engineer")["relevance"], "title")
        self.assertEqual(self.mk("Accountant")["relevance"], "description")

    def test_merge_unions_keywords_and_upgrades_relevance(self):
        jobs = {}
        je.merge(jobs, self.mk("Accountant", term="kubernetes"))
        je.merge(jobs, self.mk("Accountant", term="devops"))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs["https://x/1"]["matched"], {"kubernetes", "devops"})
        je.merge(jobs, self.mk("SRE Lead", term="sre"))  # same url, title-type match
        self.assertEqual(jobs["https://x/1"]["relevance"], "title")

    def test_merge_keeps_different_urls_apart(self):
        jobs = {}
        je.merge(jobs, self.mk("A", url="https://x/1"))
        je.merge(jobs, self.mk("B", url="https://x/2"))
        self.assertEqual(len(jobs), 2)

    def test_sort_key_title_first_then_newest(self):
        t_old = {"relevance": "title", "age_days": 9, "company": "A"}
        t_new = {"relevance": "title", "age_days": 1, "company": "B"}
        d_new = {"relevance": "description", "age_days": 0, "company": "C"}
        d_none = {"relevance": "description", "age_days": None, "company": "D"}
        self.assertEqual([j["company"] for j in sorted([d_none, d_new, t_old, t_new], key=je.sort_key)],
                         ["B", "A", "C", "D"])

    def test_add_skills_tags_and_promotes_title(self):
        j = self.mk("Docker Engineer")
        j["text"] = "Experience with Helm charts"
        plain = self.mk("Clerk", url="https://x/2")
        je.add_skills([j, plain], args())
        self.assertEqual(j["matched"], {"devops", "Docker", "Helm"})
        self.assertEqual(j["relevance"], "title")
        self.assertEqual(plain["matched"], {"devops"})

    def test_split_terms(self):
        a = args(keywords=[str(i) for i in range(10)])
        parts = je.split_terms(a, "workday", {})
        self.assertEqual(sorted(k for p in parts for k in p.keywords), sorted(a.keywords))
        self.assertEqual(len(parts), 4)
        self.assertEqual(je.split_terms(a, "greenhouse", {}), [a])
        self.assertEqual(je.split_terms(a, "eightfold", {"delay": 5}), [a])  # rate-limited: one run
        self.assertEqual(je.split_terms(args(keywords=["a", "b"]), "workday", {})[0].keywords, ["a", "b"])


class Workday(unittest.TestCase):
    GROUPS = [{"facetParameter": "locationMainGroup", "values": [
        {"facetParameter": "locations", "values": [
            {"descriptor": "India", "id": "abc123"}, {"descriptor": "Indiana", "id": "zzz"}]}]}]

    def test_find_country_facet(self):
        # the id belongs to the innermost group that holds the country value
        self.assertEqual(je.find_country_facet(self.GROUPS, "india"), ("locations", "abc123"))
        self.assertIsNone(je.find_country_facet(self.GROUPS, "Peru"))
        self.assertIsNone(je.find_country_facet(None, "India"))

    def test_fetch_workday_builds_jobs_and_skips_placeholders(self):
        calls = []

        def fake(url, body=None, headers=None, **kw):
            calls.append(body)
            if body["limit"] == 1:  # probe
                return {"facets": self.GROUPS}
            return {"total": 2, "jobPostings": [
                {"title": "DevOps Engineer", "externalPath": "/job/Pune/DevOps_R1",
                 "locationsText": "Pune", "postedOn": "Posted 2 Days Ago", "bulletFields": ["R1"]},
                {"bulletFields": ["placeholder"]}]}

        c = {"name": "Acme", "host": "acme.wd3.myworkdayjobs.com", "tenant": "acme", "site": "Ext"}
        with mock.patch.object(je, "http_json", fake):
            jobs = je.fetch_workday(c, args())
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual(j["url"], "https://acme.wd3.myworkdayjobs.com/en-US/Ext/job/Pune/DevOps_R1")
        self.assertEqual((j["age_days"], j["job_id"], j["relevance"]), (2, "R1", "title"))
        self.assertEqual(calls[1]["appliedFacets"], {"locations": ["abc123"]})

    def test_fetch_workday_text_fallback_filters_non_india(self):
        def fake(url, body=None, headers=None, **kw):
            if body["limit"] == 1:
                return {"facets": []}
            return {"total": 2, "jobPostings": [
                {"title": "SRE", "externalPath": "/job/a_1", "locationsText": "Pune"},
                {"title": "SRE", "externalPath": "/job/b_2", "locationsText": "Denver, US"}]}

        c = {"name": "Acme", "host": "h.wd1.myworkdayjobs.com", "tenant": "t", "site": "S"}
        with mock.patch.object(je, "http_json", fake):
            jobs = je.fetch_workday(c, args())
        self.assertEqual([j["location"] for j in jobs], ["Pune"])


RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Platform Engineer (Pune, IN)</title><link>https://h/job/Pune-Platform/123/?x=1</link>
<pubDate>Mon, 10 Nov 2025 00:00:00 GMT</pubDate><description>&lt;p&gt;Docker&lt;/p&gt;</description></item>
<item><title>Engineer (Berlin, DE)</title><link>https://h/job/Berlin/124/</link>
<pubDate>Mon, 10 Nov 2025 00:00:00 GMT</pubDate></item>
<item><title>No jobs currently available</title><link>https://h/</link></item>
<item><title>Ops (Festo SE & Co. KG, Bengaluru, IN)</title><link>https://h/job/B/125/</link>
<pubDate>Tue, 11 Nov 2025 00:00:00 GMT</pubDate></item>
</channel></rss>"""


class SuccessFactorsRss(unittest.TestCase):
    def test_parses_filters_and_tolerates_bare_ampersand(self):
        with mock.patch.object(je, "http_get", lambda *a, **k: RSS.encode()):
            jobs = je.fetch_sf_rss({"name": "Acme", "host": "h"}, args())
        self.assertEqual(sorted(j["job_id"] for j in jobs), ["123", "125"])
        first = next(j for j in jobs if j["job_id"] == "123")
        self.assertEqual(first["title"], "Platform Engineer")
        self.assertEqual(first["url"], "https://h/job/Pune-Platform/123/")
        self.assertEqual(first["posted"], "2025-11-10")


class NewPlatforms(unittest.TestCase):
    def test_avature_keeps_india_and_reads_location_from_posting_when_list_has_none(self):
        listing = """<article class="article"><div class="article__header__text">
            <a href="https://x.avature.net/careers/JobDetail/SRE-1/1?b=SRE">SRE</a>
            <div class="article__header__text__subtitle"><span class="list-item-location">Pune, India</span>
            <span>Posted 02-Nov-2026</span></div></div></article>
            <article class="article"><div class="article__header__text">
            <a href="https://x.avature.net/careers/JobDetail/SRE-2/2">SRE</a>
            <div class="article__header__text__subtitle"><span class="list-item-location">Paris, France</span></div>
            </div></article>
            <article class="article"><div class="article__header__text">
            <a href="https://x.avature.net/careers/JobDetail/SRE-3/3">SRE</a></div></article>"""
        posting = "<p>Job Title SRE Country India City Bengaluru Date Posted 05-Nov-2026 Job Category Eng</p>"

        def fake(url, *a, **k):
            if "SearchJobs" in url:
                return listing if "jobOffset=0" in url else ""
            return posting
        with mock.patch.object(je, "http_text", fake):
            jobs = je.fetch_avature({"name": "X", "base": "https://x.avature.net/careers"}, args())
        self.assertEqual(sorted(j["job_id"] for j in jobs), ["1", "3"])
        self.assertEqual(next(j for j in jobs if j["job_id"] == "3")["location"], "Bengaluru, India")
        self.assertEqual(next(j for j in jobs if j["job_id"] == "1")["posted"], "2026-11-02")

    def test_posting_date_is_read_when_the_list_has_none(self):
        # Tesco-style avature list without a date, iCIMS list without a date: the posting page has JSON-LD
        ld = '<script type="application/ld+json">{"datePosted": "2026-07-24T00:00:00+00:00"}</script>'
        listing = """<article class="article"><div class="article__header__text">
            <a href="https://x.avature.net/careers/JobDetail/SRE-1/1">SRE</a>
            <div class="article__header__text__subtitle"><span class="list-item-location">Pune, India</span>
            </div></div></article>"""
        with mock.patch.object(je, "http_text", lambda url, *a, **k: listing if "SearchJobs" in url and "jobOffset=0" in url
                               else "" if "SearchJobs" in url else ld):
            jobs = je.fetch_avature({"name": "X", "base": "https://x.avature.net/careers"}, args())
        self.assertEqual(jobs[0]["posted"], "2026-07-24")
        self.assertEqual(je.page_posted("<html></html>"), None)

        card = ('<li class="iCIMS_JobCardItem"><a href="https://h/jobs/9/sre/job?in_iframe=1">'
                '<h3>SRE</h3></a><span>Job Locations</span><span >IN-Pune</span></li>')
        with mock.patch.object(je, "http_text", lambda url, *a, **k: card if "search" in url else ld):
            jobs = je.fetch_icims({"name": "X", "hosts": ["h"]}, args())
        self.assertEqual((jobs[0]["posted"], jobs[0]["location"]), ("2026-07-24", "IN-Pune"))

    def test_workday_start_date_fills_missing_posted_date_and_location(self):
        job = je.make_job("X", "workday", "SRE", "", "", None, "u", "1", "sre", detail="https://d")
        detail = {"jobPostingInfo": {"jobDescription": "k8s", "startDate": "2026-11-23", "location": "Pune, IN"}}
        with mock.patch.object(je, "http_json", lambda *a, **k: detail):
            je.add_skills([job], args())
        self.assertEqual((job["posted"], job["location"]), ("2026-11-23", "Pune, IN"))
        self.assertEqual(job["age_days"], (date.today() - date(2026, 11, 23)).days)

    def test_cornerstone_filters_on_country_code_and_builds_urls(self):
        page = '{"cloud":"https://eu.api.csod.com/","token":"tok"}'
        rows = {"data": {"totalCount": 1, "requisitions": [{
            "requisitionId": 7, "displayJobTitle": "Platform Engineer", "postingEffectiveDate": "11/12/2026",
            "locations": [{"city": "Pune", "state": "MH", "country": "IN"}], "externalDescription": "<b>k8s</b>"}]}}
        sent = {}

        def fake_json(url, body=None, headers=None, **k):
            sent.update(body=body, headers=headers)
            return rows
        with mock.patch.object(je, "http_text", lambda *a, **k: page), mock.patch.object(je, "http_json", fake_json):
            jobs = je.fetch_cornerstone({"name": "C", "host": "c.csod.com", "site": 3, "corp": "c"}, args())
        self.assertEqual(sent["body"]["countryCodes"], ["IN"])
        self.assertEqual(sent["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(jobs[0]["url"], "https://c.csod.com/ux/ats/careersite/3/home/requisition/7?c=c")
        self.assertEqual(jobs[0]["location"], "Pune, MH, India")
        self.assertEqual(jobs[0]["posted"], "2026-11-12")

    def test_apple_skips_non_india_locations(self):
        rows = {"res": {"totalRecords": 2, "searchResults": [
            {"postingTitle": "SRE", "positionId": "1", "transformedPostingTitle": "sre", "reqId": "1-1",
             "postDateInGMT": "2026-11-20T00:00:00Z", "locations": [{"name": "Hyderabad", "countryName": "India"}]},
            {"postingTitle": "SRE", "positionId": "2", "transformedPostingTitle": "sre", "reqId": "2-1",
             "postDateInGMT": "2026-11-20T00:00:00Z", "locations": [{"name": "Austin", "countryName": "United States"}]}]}}
        with mock.patch.object(je, "http_json", lambda *a, **k: rows):
            jobs = je.fetch_apple({"name": "Apple"}, args())
        self.assertEqual([j["job_id"] for j in jobs], ["1-1"])
        self.assertEqual(jobs[0]["location"], "Hyderabad, India")

    def test_sap_reads_embedded_job_list_and_matches_title_and_team(self):
        page = ('{\\"jobs\\":[{\\"id\\":\\"7\\",\\"title\\":\\"Platform Engineer\\",\\"url\\":\\"/en/jobs/7/pe/\\",'
                '\\"score\\":1.5,\\"teams\\":\\"Engineering\\",\\"locations\\":\\"Bangalore, India\\",'
                '\\"displayDate\\":\\"2026-11-20T01:00:00.000Z\\"},{\\"id\\":\\"8\\",\\"title\\":\\"Sales Lead\\",'
                '\\"url\\":\\"/en/jobs/8/sl/\\",\\"score\\":1.1,\\"teams\\":\\"Sales\\",'
                '\\"locations\\":\\"Pune, India\\",\\"displayDate\\":\\"2026-11-19T01:00:00.000Z\\"}]}')
        pages = iter([page, ""])
        with mock.patch.object(je, "http_text", lambda *a, **k: next(pages)):
            jobs = je.fetch_sap({"name": "SAP"}, args(keywords=["engineer"]))
        self.assertEqual([j["job_id"] for j in jobs], ["7"])
        self.assertEqual(jobs[0]["url"], "https://jobs.sap.com/en/jobs/7/pe/")
        self.assertEqual(jobs[0]["posted"], "2026-11-20")


class HttpGet(unittest.TestCase):
    def http_error(self, code):
        e = urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b"body"))
        e.close = mock.Mock(wraps=e.close)
        return e

    def test_closes_http_errors_so_sockets_do_not_leak(self):
        e = self.http_error(404)
        with mock.patch("urllib.request.urlopen", side_effect=e):
            with self.assertRaises(urllib.error.HTTPError):
                je.http_get("https://x")
        e.close.assert_called()

    def test_retries_server_errors_then_succeeds(self):
        errs = [self.http_error(521), self.http_error(521)]
        ok = mock.MagicMock()
        ok.__enter__.return_value.read.return_value = b"fine"
        ok.__enter__.return_value.geturl.return_value = "https://x"
        with mock.patch("urllib.request.urlopen", side_effect=errs + [ok]), \
                mock.patch.object(je.time, "sleep"):
            self.assertEqual(je.http_get("https://x"), b"fine")
        for e in errs:
            e.close.assert_called()

    def test_no_retry_on_404(self):
        with mock.patch("urllib.request.urlopen", side_effect=self.http_error(404)) as m:
            with self.assertRaises(urllib.error.HTTPError):
                je.http_get("https://x")
        self.assertEqual(m.call_count, 1)

    def test_maintenance_redirect_fails_fast_with_a_clear_message(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.geturl.return_value = "https://community.workday.com/maintenance-page"
        with mock.patch("urllib.request.urlopen", return_value=resp) as m:
            with self.assertRaises(je.SiteDown) as cm:
                je.http_get("https://visa.wd5.myworkdayjobs.com/wday/cxs/visa/Jobs/jobs")
        self.assertEqual(m.call_count, 1)  # no retries
        self.assertIn("maintenance", str(cm.exception))

    def test_normal_redirect_is_not_an_outage(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.geturl.return_value = "https://example.com/new-location"
        resp.read.return_value = b"ok"
        with mock.patch("urllib.request.urlopen", return_value=resp):
            self.assertEqual(je.http_get("https://example.com/old"), b"ok")

    def test_gives_up_after_retries(self):
        with mock.patch("urllib.request.urlopen", side_effect=self.http_error(503)) as m, \
                mock.patch.object(je.time, "sleep"):
            with self.assertRaises(urllib.error.HTTPError):
                je.http_get("https://x", retries=3)
        self.assertEqual(m.call_count, 3)


class RateLimitedEightfold(unittest.TestCase):
    def test_one_combined_search_and_keywords_read_from_titles(self):
        calls = []

        def fake(url, delay):
            calls.append(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["query"][0])
            pos = [{"id": 1, "name": "Senior SRE", "locations": ["Bengaluru, India"], "postedTs": 0,
                    "canonicalPositionUrl": "https://ms/1"},
                   {"id": 2, "name": "Cloud Architect", "locations": ["Pune, India"], "postedTs": 0,
                    "canonicalPositionUrl": "https://ms/2"}]
            return {"data": {"count": 2, "positions": pos}}

        c = {"name": "Microsoft", "host": "h", "domain": "d", "api": "pcsx", "delay": 5}
        a = args(keywords=["sre", "devops", "forward deployed engineer"])
        with mock.patch.object(je, "eightfold_pcsx", side_effect=fake):
            jobs = je.fetch_eightfold(c, a)
        self.assertEqual(calls, ['sre OR devops OR "forward deployed engineer"'])
        by_url = {j["url"]: j for j in jobs}
        self.assertEqual(by_url["https://ms/1"]["matched"], {"sre"})
        self.assertEqual(by_url["https://ms/1"]["relevance"], "title")
        self.assertEqual(by_url["https://ms/2"]["matched"], {je.UNION_TERM})


class Pacing(unittest.TestCase):
    def test_requests_to_one_host_are_spaced_apart_even_from_parallel_threads(self):
        je._pace_next.clear()
        sleeps = []
        with mock.patch.object(je.time, "sleep", side_effect=sleeps.append), \
                mock.patch.object(je.time, "monotonic", return_value=100.0):
            for _ in range(4):
                je.pace("host-a", 5)
            je.pace("host-b", 5)  # another host is not delayed
        self.assertEqual(sleeps, [5.0, 10.0, 15.0])

    def test_no_delay_configured_means_no_pacing(self):
        je._pace_next.clear()
        with mock.patch.object(je.time, "sleep") as s:
            je.pace("host-a", 0)
        s.assert_not_called()


class Outputs(unittest.TestCase):
    def test_state_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "s.json"
            self.assertEqual(je.load_state(p), {})
            p.write_text("{not json")
            self.assertEqual(je.load_state(p), {})
            p.write_text('{"u": {"first_seen": "2026-01-01"}}')
            self.assertEqual(je.load_state(p)["u"]["first_seen"], "2026-01-01")

    def test_write_outputs_adds_sector_column_only_when_tagged(self):
        job = je.make_job("Acme", "workday", "SRE", "Pune", "Today", 0, "https://x/1", 1, "sre")
        job["first_seen"] = "2026-01-01"
        with tempfile.TemporaryDirectory() as d:
            je.write_outputs(Path(d) / "a", [job], [])
            with open(Path(d) / "a" / "jobs.csv") as f:
                plain = next(csv.reader(f))
            self.assertNotIn("sector", plain)
            je.write_outputs(Path(d) / "b", [{**job, "sector": "fintech"}],
                             [{"name": "M", "platform": "p", "url": "https://m"}], "All")
            with open(Path(d) / "b" / "jobs.csv") as f:
                rows = list(csv.DictReader(f))
            self.assertEqual(rows[0]["sector"], "fintech")
            md = (Path(d) / "b" / "jobs.md").read_text()
            self.assertIn("[SRE](https://x/1)", md)
            self.assertIn("M (p): https://m", md)

    def test_print_table_aligns_columns(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            je.print_table(["#", "Name"], [[1, "a"], [22, "long name"]])
        lines = buf.getvalue().splitlines()
        self.assertEqual(len({len(x) for x in lines}), 1)


class MainRun(unittest.TestCase):
    """End-to-end run of main() with fake fetchers: retry of failed sites, failure reporting."""

    def run_main(self, fetcher, extra=()):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "c.json"
            cfg.write_text(json.dumps({"workday": [
                {"name": "Good", "host": "h", "tenant": "t", "site": "s", "sector": "fintech"},
                {"name": "Flaky", "host": "h", "tenant": "t", "site": "s", "sector": "energy"}]}))
            argv = ["prog", "--config", str(cfg), "--output-dir", str(Path(d) / "out"),
                    "--show-all", "--retry-wait", "0", "--keywords", "devops", "--skills", "Docker",
                    *extra]
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "argv", argv), \
                    mock.patch.dict(je.FETCHERS, {"workday": fetcher}), \
                    redirect_stdout(out), redirect_stderr(err):
                je.main("Test", str(cfg), str(Path(d) / "out"))
            html = (Path(d) / "out" / "latest.html").read_text(encoding="utf-8")
            with open(next((Path(d) / "out").glob("*/jobs.csv"))) as f:
                rows = list(csv.DictReader(f))
            return out.getvalue(), err.getvalue(), html, rows

    @staticmethod
    def job(name):
        return je.make_job(name, "workday", "DevOps Engineer", "Pune", "Today", 0,
                           f"https://x/{name}", 1, "devops")

    def test_failed_site_is_retried_and_recovers(self):
        seen = {"Flaky": 0}

        def fetcher(c, a):
            if c["name"] == "Flaky":
                seen["Flaky"] += 1
                if seen["Flaky"] == 1:
                    raise RuntimeError("HTTP Error 521")
            return [self.job(c["name"])]

        out, err, _html, rows = self.run_main(fetcher)
        self.assertIn("2 sites scanned, 0 failed, 2 matching", out)
        self.assertIn("retrying", err)
        self.assertEqual(sorted(r["company"] for r in rows), ["Flaky", "Good"])
        self.assertEqual({r["company"]: r["sector"] for r in rows}, {"Flaky": "energy", "Good": "fintech"})

    def test_remote_only_keeps_remote_jobs_and_tags_work_mode(self):
        def fetcher(c, a):
            loc = "Remote - India" if c["name"] == "Good" else "Pune"
            return [je.make_job(c["name"], "workday", "DevOps Engineer", loc, "Today", 0,
                                f"https://x/{c['name']}", 1, "devops")]

        out, _err, html, rows = self.run_main(fetcher, ["--remote-only"])
        self.assertEqual([(r["company"], r["work_mode"]) for r in rows], [("Good", "remote")])
        self.assertIn('id="mode"', html)
        self.assertIn('id="reset"', html)
        self.assertIn('<option value="today"', html)  # "Today" lives in the age dropdown ...
        self.assertNotIn('id="onlynew"', html)        # ... not in a separate checkbox

    def test_work_mode_column_without_remote_only(self):
        def fetcher(c, a):
            loc = "Pune (Hybrid)" if c["name"] == "Good" else "Pune"
            return [je.make_job(c["name"], "workday", "DevOps Engineer", loc, "Today", 0,
                                f"https://x/{c['name']}", 1, "devops")]

        _out, _err, _html, rows = self.run_main(fetcher)
        self.assertEqual({r["company"]: r["work_mode"] for r in rows}, {"Good": "hybrid", "Flaky": ""})

    def test_shards_are_dumped_and_merged_into_one_report(self):
        def fetcher(c, a):
            if c["name"] == "Flaky":
                raise RuntimeError("HTTP Error 521")
            return [self.job(c["name"])]

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "c.json"
            cfg.write_text(json.dumps({"workday": [
                {"name": "Good", "host": "h", "tenant": "t", "site": "s", "sector": "fintech"},
                {"name": "Flaky", "host": "h", "tenant": "t", "site": "s", "sector": "energy"},
                {"name": "Third", "host": "h", "tenant": "t", "site": "s", "sector": "energy"}]}))
            base = ["prog", "--config", str(cfg), "--output-dir", str(Path(d) / "out"), "--show-all",
                    "--retry-failed", "0", "--keywords", "devops", "--skills", "Docker"]
            for argv in (base + ["--shard", "0/2", "--dump-jobs", f"{d}/shards/0.json"],
                         base + ["--shard", "1/2", "--dump-jobs", f"{d}/shards/1.json"]):
                with mock.patch.object(sys, "argv", argv), mock.patch.dict(je.FETCHERS, {"workday": fetcher}), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    je.main("Test", str(cfg), str(Path(d) / "out"))
            self.assertFalse((Path(d) / "out").exists())  # a shard writes neither state nor report
            out = io.StringIO()
            with mock.patch.object(sys, "argv", base + ["--merge-jobs", f"{d}/shards"]), \
                    mock.patch.dict(je.FETCHERS, {"workday": lambda c, a: self.fail("merge must not scrape")}), \
                    redirect_stdout(out), redirect_stderr(io.StringIO()):
                je.main("Test", str(cfg), str(Path(d) / "out"))
            with open(next((Path(d) / "out").glob("*/jobs.csv"))) as f:
                rows = list(csv.DictReader(f))
        self.assertIn("3 sites scanned, 1 failed, 2 matching", out.getvalue())
        self.assertEqual(sorted(r["company"] for r in rows), ["Good", "Third"])
        self.assertEqual({r["company"]: r["sector"] for r in rows}, {"Good": "fintech", "Third": "energy"})

    def test_descriptions_of_known_jobs_are_not_fetched_again(self):
        calls = []

        def http(url, *a, **k):
            calls.append(url)
            return {"jobPostingInfo": {"jobDescription": "We run Docker. Remote work from home.",
                                       "startDate": "2026-11-20"}}

        def fetcher(c, a):
            return [je.make_job(c["name"], "workday", "DevOps Engineer", "", "", None,
                                f"https://x/{c['name']}", 1, "devops", detail=f"https://x/d/{c['name']}")]

        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "c.json"
            cfg.write_text(json.dumps({"workday": [
                {"name": "Good", "host": "h", "tenant": "t", "site": "s"}]}))
            argv = ["prog", "--config", str(cfg), "--output-dir", str(Path(d) / "out"), "--show-all",
                    "--max-age-days", "3650", "--keywords", "devops", "--skills", "Docker"]
            results = []
            for _ in range(2):
                with mock.patch.object(sys, "argv", argv), mock.patch.dict(je.FETCHERS, {"workday": fetcher}), \
                        mock.patch.object(je, "http_json", http), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    je.main("Test", str(cfg), str(Path(d) / "out"))
                with open(next((Path(d) / "out").glob("*/jobs.csv"))) as f:
                    results.append(list(csv.DictReader(f))[0])
        self.assertEqual(len(calls), 1)  # second run reads skills, work mode and date from the state
        self.assertEqual({k: results[0][k] for k in ("matched", "work_mode", "posted")},
                         {k: results[1][k] for k in ("matched", "work_mode", "posted")})
        self.assertEqual((results[1]["work_mode"], results[1]["posted"]), ("remote", "2026-11-20"))
        self.assertIn("Docker", results[1]["matched"])

    def test_persistent_failure_is_reported(self):
        def fetcher(c, a):
            if c["name"] == "Flaky":
                raise RuntimeError("HTTP Error 521")
            return [self.job(c["name"])]

        out, err, html, rows = self.run_main(fetcher, ["--retry-failed", "0"])
        self.assertIn("2 sites scanned, 1 failed, 1 matching", out)
        self.assertIn("Failed (1): Flaky", out)
        self.assertIn("[error] Flaky: HTTP Error 521", err)
        self.assertIn("Flaky", html)
        self.assertEqual([r["company"] for r in rows], ["Good"])


class KeywordsRequired(unittest.TestCase):
    def run_main(self, argv, tty=False):
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "c.json"
            cfg.write_text(json.dumps({"workday": [{"name": "Good", "host": "h", "tenant": "t", "site": "s"}]}))
            seen = []

            def fetcher(c, a):
                seen.append(list(a.keywords))
                return []

            full = ["prog", "--config", str(cfg), "--output-dir", str(Path(d) / "out"), *argv]
            with mock.patch.object(sys, "argv", full), mock.patch.dict(je.FETCHERS, {"workday": fetcher}), \
                    mock.patch.object(sys.stdin, "isatty", return_value=tty), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                je.main("T", str(cfg), str(Path(d) / "out"))
            return seen

    def test_non_interactive_run_without_keywords_stops(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_main([])
        self.assertEqual(cm.exception.code, 2)

    def test_terminal_run_asks_for_keywords(self):
        with mock.patch("builtins.input", return_value="java, testing"):
            seen = self.run_main([], tty=True)
        self.assertEqual(seen, [["java", "testing"]])

    def test_keywords_come_from_the_command_line_unchanged(self):
        seen = self.run_main(["--keywords", "sre,devops,kubernetes,mlops"])
        self.assertEqual(seen, [["sre", "devops", "kubernetes", "mlops"]])


class Configs(unittest.TestCase):
    """Every sectors/*/companies.json must be loadable by the engine."""

    @classmethod
    def setUpClass(cls):
        cls.files = sorted((BASE / "sectors").glob("*/companies.json"))
        cls.data = {f.parent.name: json.loads(f.read_text(encoding="utf-8")) for f in cls.files}

    def test_there_are_sector_configs(self):
        self.assertGreater(len(self.files), 10)

    def test_kinds_known_and_entries_named(self):
        for sector, cfg in self.data.items():
            for kind, items in cfg.items():
                self.assertTrue(kind in je.FETCHERS or kind == "manual", f"{sector}: unknown ATS '{kind}'")
                for c in items:
                    self.assertTrue(c.get("name"), f"{sector}/{kind}: entry without name")

    def test_required_fields_per_ats(self):
        need = {"workday": ("host", "tenant", "site"), "oracle": ("host", "site"), "sf_rss": ("host",),
                "greenhouse": ("board",), "smartrecruiters": ("company_id",), "lever": ("company_id",),
                "jibe": ("host",), "eightfold": ("host", "domain"), "radancy": ("host", "path"),
                "phenom": ("host", "lang", "page_id", "region"), "avature": ("base",),
                "cornerstone": ("host", "site", "corp"), "taleo": ("host",), "manual": ("platform", "url")}
        for sector, cfg in self.data.items():
            for kind, items in cfg.items():
                for c in items:
                    for f in need.get(kind, ()):
                        self.assertTrue(c.get(f), f"{sector}/{kind}/{c['name']}: missing '{f}'")

    def test_no_company_in_two_sectors_or_with_a_shared_tenant(self):
        names, ids = {}, {}
        for sector, cfg in self.data.items():
            for kind, items in cfg.items():
                for c in items:
                    key_name = (kind, c["name"])
                    if key_name in names and names[key_name] != sector:
                        # known case: Intertek is listed twice inside one sector only
                        self.fail(f"{c['name']} is in {names[key_name]} and {sector}")
                    names[key_name] = sector
                    ident = {"workday": (c.get("host"), c.get("tenant"), c.get("site")),
                             "greenhouse": (c.get("board"),),
                             "smartrecruiters": (c.get("company_id"),),
                             "lever": (c.get("company_id"),)}.get(kind)
                    if ident:
                        ident = (kind,) + ident
                        other = ids.setdefault(ident, c["name"])
                        self.assertEqual(other, c["name"], f"{ident} used by '{other}' and '{c['name']}'")

    def test_policy_exclusions_not_scraped(self):
        excluded = json.loads((BASE / "sectors" / "excluded.json").read_text(encoding="utf-8"))
        flat = {x.lower() if isinstance(x, str) else x.get("name", "").lower()
                for x in (excluded if isinstance(excluded, list)
                          else [i for v in excluded.values() for i in (v if isinstance(v, list) else [])])}
        scraped = {c["name"].lower() for cfg in self.data.values() for k, v in cfg.items()
                   if k != "manual" for c in v}
        # service-type configs the user has not yet decided on are tracked in the README
        pending = {"worley", "wood", "kbr", "fluor", "parsons", "fugro", "bureau veritas", "dnv",
                   "intertek", "sgs", "tüv süd", "aon", "gallagher", "marsh mclennan",
                   # also on the excluded list but scraped: needs the user's decision
                   "technip energies", "johnson controls", "labcorp"}
        self.assertEqual(sorted((flat & scraped) - pending), [])


class ScrapeAll(unittest.TestCase):
    def test_combine_tags_sector(self):
        import scrape_all
        merged = scrape_all.combine(["fintech", "energy"])
        self.assertTrue(merged["workday"])
        self.assertTrue(all(c["sector"] in ("fintech", "energy") for c in merged["workday"]))
        self.assertIn("fintech", scrape_all.ready_sectors())


if __name__ == "__main__":
    unittest.main()
