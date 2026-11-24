"""End-to-end tests of the HTML report in a real browser (headless Google Chrome / Chromium).

The report is built with the real template, a driver script is appended, Chrome opens the file, the driver
clicks and types like a user (header clicks, dropdowns, search box, Reset) and returns what the page shows. Every
step is compared with an independent model written here in Python: which rows are visible, in which order, with
which cell texts, links, tags and counts. Skipped when Chrome is not installed (set CHROME_BIN to point at it).

    python3 -m unittest tests.test_html -v      (from the project folder)
"""
import html
import io
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import jobs_engine as je  # noqa: E402

CHROME = next((p for p in (
    os.environ.get("CHROME_BIN", ""),
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    shutil.which("google-chrome") or "", shutil.which("chromium") or "",
    shutil.which("chromium-browser") or "", shutil.which("chrome") or "") if p and Path(p).exists()), None)

TODAY = "2026-11-24"
HEADERS = ["Posted", "Sector", "Company", "Role", "Location", "Match", "Mode", "Keywords", "Platform"]
HEADERS_NO_SECTOR = [h for h in HEADERS if h != "Sector"]
SORT_FIELD = {"Posted": "age_days", "Sector": "sector", "Company": "company", "Role": "title",
              "Location": "location", "Match": "relevance", "Mode": "work_mode", "Keywords": "kw",
              "Platform": "platform"}
FILTER_IDS = ["q", "sector", "company", "city", "kwsel", "platform", "mode", "match", "age"]
NO_CITY = "Other / not stated"
MODE_LABEL = {"remote": "Remote / WFH", "hybrid": "Hybrid", "": ""}


# ----------------------------------------------------------------------------- test data
def mk(i, sector, company, title, location, age, relevance, platform, mode, kws, url=None, new=False):
    posted = "" if age is None else (date.fromisoformat(TODAY) - timedelta(days=age)).isoformat()
    j = je.make_job(company, platform, title, location, posted, age,
                    f"https://example.com/{i}" if url is None else url, f"R{i}", kws[0])
    j["matched"] = set(kws)
    j["relevance"] = relevance
    j.update(sector=sector, work_mode=mode, first_seen=TODAY if new else "2026-11-01")
    return j


# the Schneider case from the bug report, plus a spread of everything the filters look at
FIXED = [
    ("energy", "Schneider Electric", "Senior DevOps Engineer", "Bengaluru, India", 0, "title", "jibe", "", ["devops"]),
    ("energy", "Schneider Electric", "Lead SRE", "Hyderabad, India", 12, "title", "jibe", "hybrid", ["sre"]),
    ("energy", "Schneider Electric", "AI Engineer II", "Pune, India", None, "description", "jibe", "", ["ai engineer"]),
    ("energy", "Schneider Electric", "Cloud Platform Owner", "Bengaluru, India", 3, "description", "jibe", "remote",
     ["kubernetes", "devops"]),
    ("energy", "Schneider Electric", "Firmware Engineer", "Chennai, India", 14, "description", "jibe", "", ["devops"]),
    ("software_cloud", "Microsoft", "Site Reliability Engineer 2", "Hyderabad, India", 1, "title", "eightfold", "", ["sre"]),
    ("software_cloud", "Microsoft", "Principal Engineer", "Bangalore, India", 1, "description", "eightfold", "hybrid",
     ["(search)"]),
    ("software_cloud", "Microsoft", "MLOps Engineer", "Noida, India", 7, "title", "eightfold", "remote", ["mlops"]),
    ("fintech", "Citi", "Kubernetes Admin", "Mumbai, India", 5, "title", "workday", "", ["kubernetes"]),
    ("fintech", "Citi", "DevOps Lead", "Pune, India; Chennai, India", 0, "title", "workday", "hybrid", ["devops"]),
    ("fintech", "Citi", "Platform Engineer", "Gurgaon, India", 14, "description", "workday", "", ["sre", "devops"]),
    ("fintech", "Goldman Sachs", "SRE Associate", "Bengaluru, India", None, "title", "goldman", "", ["sre"]),
    ("fintech", "Goldman Sachs", "Infra Engineer", "Hyderabad, India", None, "description", "goldman", "remote", ["sre"]),
    ("cybersecurity", "Zscaler", "DevOps Engineer", "Remote - India", 2, "title", "greenhouse", "remote", ["devops"]),
    ("cybersecurity", "Zscaler", "Cloud SRE", "Pune, India", 9, "title", "greenhouse", "", ["sre"]),
    ("cybersecurity", "Zscaler", "Security Engineer", "India", 4, "description", "greenhouse", "", ["devops"]),
]

# text that must be shown literally and must never run or become markup
HOSTILE_TITLES = [
    "</script><img src=x onerror=alert(1)>", "<b>bold</b> & \"quotes\" 'single'", "back\\slash \\u0041 \\n",
    "<!-- comment --> __DATA__ __TODAY__", "Ünïcödé – 日本語 – déjà vu", "line\u2028separator", "a" * 300,
    "{{7*7}} ${7*7} %s %d", "<script>window.__pwned = 1</script>",
]


def random_jobs(n, seed):
    rnd = random.Random(seed)
    sectors = ["energy", "fintech", "software_cloud", "cybersecurity", "automotive"]
    companies = {s: [f"{s[:3].title()} Co {k}" for k in range(1, 4)] for s in sectors}
    platforms = ["workday", "jibe", "greenhouse", "oracle", "eightfold", "sf_rss"]
    locations = ["Bengaluru, India", "Bangalore, India", "Pune, India; Chennai, India", "Hyderabad, India",
                 "Mumbai, India", "Noida, India; Gurugram, India", "India", "Remote - India", "Kolkata, India",
                 "Pune", "", "Delhi, India", "Indiana, USA"]
    titles = ["DevOps Engineer", "SRE", "Site Reliability Engineer", "Kubernetes Platform Engineer", "MLOps Lead",
              "AI Engineer", "Cloud Architect", "Senior DevOps Engineer", "Platform Engineer"] + HOSTILE_TITLES
    kws = ["devops", "sre", "kubernetes", "mlops", "ai engineer"]
    jobs = []
    for i in range(n):
        s = rnd.choice(sectors)
        jobs.append(mk(i, s, rnd.choice(companies[s]), rnd.choice(titles), rnd.choice(locations),
                       rnd.choice([None, 0, 0, 1, 2, 3, 4, 5, 7, 8, 10, 13, 14]),
                       rnd.choice(["title", "description"]), rnd.choice(platforms),
                       rnd.choice(["", "", "", "remote", "hybrid"]), rnd.sample(kws, rnd.randint(1, 3)),
                       new=rnd.random() < 0.2))
    return jobs


def fixed_jobs():
    jobs = [mk(i, *r, new=i % 3 == 0) for i, r in enumerate(FIXED)]
    n = len(jobs)
    for k, t in enumerate(HOSTILE_TITLES):
        jobs.append(mk(n + k, "fintech", "Hostile & <Co>", t, "Pune, India", k % 5, "title", "workday", "",
                       ["devops"]))
    n = len(jobs)
    jobs.append(mk(n, "fintech", "Odd Links", "javascript link", "Pune, India", 1, "title", "workday", "", ["sre"],
                   url="javascript:alert(1)"))
    jobs.append(mk(n + 1, "fintech", "Odd Links", "empty link", "Pune, India", 1, "title", "workday", "", ["sre"],
                   url=""))
    return jobs


# ----------------------------------------------------------------------------- the model (what a user must see)
def cities(j):
    return je.cities_of(j["location"], Namespace(country="India"))


def kw(j):
    return ", ".join(sorted(j["matched"]))


class Model:
    """The report's behaviour written from its specification, independent of the page's JavaScript."""

    def __init__(self, jobs):
        self.jobs = jobs
        self.reset()

    def reset(self):
        self.f = {k: "" for k in FILTER_IDS}
        self.sort_key, self.sort_dir = None, 1

    def set(self, id_, value):
        self.f[id_] = value

    def click(self, label):
        key = SORT_FIELD[label]
        self.sort_dir = -self.sort_dir if self.sort_key == key else 1
        self.sort_key = key

    def keep(self, j):
        f = self.f
        if f["sector"] and j["sector"] != f["sector"]:
            return False
        if f["company"] and j["company"] != f["company"]:
            return False
        if f["match"] and j["relevance"] != f["match"]:
            return False
        if f["city"] and f["city"] not in (cities(j) or [NO_CITY]):
            return False
        if f["kwsel"] and f["kwsel"] not in j["matched"]:
            return False
        if f["platform"] and j["platform"] != f["platform"]:
            return False
        if f["mode"] == "onsite" and j["work_mode"]:
            return False
        if f["mode"] in ("remote", "hybrid") and j["work_mode"] != f["mode"]:
            return False
        if f["age"] == "today" and j["age_days"] != 0:
            return False
        if f["age"] not in ("", "today") and (j["age_days"] is None or j["age_days"] > int(f["age"])):
            return False
        q = f["q"].lower().strip()
        if q and q not in " ".join([j["title"], j["company"], j["location"], " ".join(cities(j)),
                                    kw(j)]).lower():
            return False
        return True

    def visible(self):
        rows = [i for i, j in enumerate(self.jobs) if self.keep(j)]
        k = self.sort_key
        if k:
            def val(i):
                return kw(self.jobs[i]) if k == "kw" else self.jobs[i][k]
            if k == "age_days":  # undated jobs last, whichever way it is sorted
                undated = [i for i in rows if val(i) is None]
                rows = sorted([i for i in rows if val(i) is not None], key=val, reverse=self.sort_dir == -1) + undated
            else:  # python's sort is stable, also with reverse=True: equal values keep the report order
                rows = sorted(rows, key=val, reverse=self.sort_dir == -1)
        return rows


def expected_cells(j, with_sector=True):
    cells = [j["posted"]]
    if with_sector:
        cells.append((j["sector"] or "").replace("_", " "))
    cells += [j["company"], j["title"] + ("NEW" if j["first_seen"] == TODAY else ""), j["location"],
              j["relevance"], MODE_LABEL[j["work_mode"]], kw(j), j["platform"]]
    return cells


def href(j):
    return j["url"] if re.match(r"https?://", j["url"]) else None


# ----------------------------------------------------------------------------- browser harness
ERROR_HOOK = ("<script>window.alert=function(){window.__pwned=(window.__pwned||0)+1};window.__errs=[];window.addEventListener('error',e=>__errs.push(String(e.message)));"
              "window.addEventListener('unhandledrejection',e=>__errs.push(String(e.reason)));</script>")

DRIVER = r"""
<script>
(function () {
  const SC = __SCENARIOS__, COMPACT = __COMPACT__, out = [];
  const IDS = ["q","sector","company","city","kwsel","platform","mode","match","age"];
  const fire = (id) => $(id).dispatchEvent(new Event(id === "q" ? "input" : "change"));
  function snap() {
    const rows = [...document.querySelectorAll("#body tr")].map(tr => {
      const a = tr.querySelector("a");
      if (COMPACT) return parseInt((a.getAttribute("href") || "/-1").split("/").pop(), 10);
      return {cells: [...tr.children].map(td => td.textContent), href: a.getAttribute("href"),
              target: a.getAttribute("target"), rel: a.getAttribute("rel"), isNew: !!tr.querySelector(".tag.new"),
              tags: [...tr.querySelectorAll(".tag")].map(t => t.className),
              injected: tr.querySelectorAll("img,script,iframe,b,style").length};
    });
    return {rows, count: $("count").textContent, none: !$("none").hidden,
            sel: Object.fromEntries(IDS.map(id => [id, $(id).value]))};
  }
  SC.forEach(s => {
    const snaps = [];
    IDS.forEach(id => { $(id).value = ""; }); sortKey = null; sortDir = 1; render();  // every scenario starts fresh
    s.steps.forEach((st, n) => {
      if (st.set) Object.entries(st.set).forEach(([id, v]) => { $(id).value = v; fire(id); });
      if (st.click) [...document.querySelectorAll("#head th")].find(th => th.textContent === st.click).click();
      if (st.reset) $("reset").click();
      if (!COMPACT || n === s.steps.length - 1) snaps.push(snap());
    });
    out.push({name: s.name, snaps});
  });
  const pre = document.createElement("pre"); pre.id = "result";
  pre.textContent = JSON.stringify({scenarios: out, errors: window.__errs,
    title: document.title, h1: document.querySelector("h1").textContent, sub: $("sub").textContent,
    todayopt: $("todayopt").textContent, sectorHidden: $("sector").hidden,
    headers: [...document.querySelectorAll("#head th")].map(th => th.textContent),
    options: Object.fromEntries(IDS.slice(1).map(id => [id, [...$(id).options].map(o => o.value)])),
    failed: {hidden: $("failed").hidden, text: $("failed").textContent},
    manual: [...document.querySelectorAll("#manual li")].map(li => { const a = li.querySelector("a");
      return {text: li.textContent, href: a.getAttribute("href"), target: a.getAttribute("target"), rel: a.getAttribute("rel")}; }),
    headInjected: document.querySelectorAll("header img, header b, header script").length,
    pwned: window.__pwned === undefined ? null : window.__pwned});
  document.body.append(pre);
})();
</script>
"""


def run_in_chrome(page, scenarios, compact=False):
    """Open `page` in headless Chrome after adding the driver; returns the driver's result dict."""
    driver = (DRIVER.replace("__SCENARIOS__", json.dumps(scenarios).replace("<", "\\u003c"))
              .replace("__COMPACT__", "true" if compact else "false"))
    page = page.replace("<head>", "<head>" + ERROR_HOOK, 1).replace("</body>", driver + "</body>")
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "report.html"
        f.write_text(page, encoding="utf-8")
        proc = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
                               "--virtual-time-budget=20000", "--dump-dom", f.as_uri()],
                              capture_output=True, text=True, timeout=240)
    dom = proc.stdout
    start = dom.find('<pre id="result">')
    if start < 0:
        raise AssertionError(f"driver did not run: {proc.stderr[-800:]}")
    end = dom.index("</pre>", start)
    return json.loads(html.unescape(dom[start + len('<pre id="result">'):end]))


def build_page(jobs, manual=(), failed=None, keywords=("devops", "sre"), sector="All sectors"):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "report.html"
        je.write_html(path, jobs, list(manual), TODAY,
                      Namespace(sector=sector, keywords=list(keywords), country="India"), failed or {})
        return path.read_text(encoding="utf-8")


def option_values(jobs):
    return {
        "sector": [""] + sorted({j["sector"] for j in jobs}),
        "company": [""] + sorted({j["company"] for j in jobs}),
        "city": [""] + sorted({c for j in jobs for c in (cities(j) or [NO_CITY])}),
        "kwsel": [""] + sorted({k for j in jobs for k in j["matched"]}),
        "platform": [""] + sorted({j["platform"] for j in jobs}),
        "mode": ["", "remote", "hybrid", "onsite"],
        "match": ["", "title", "description"],
        "age": ["", "today", "1", "3", "7", "14"],
    }


def random_scenarios(jobs, n, seed, max_steps=6):
    rnd = random.Random(seed)
    opts = option_values(jobs)
    terms = ["sre", "SRE", " devops ", "pune", "CHENNAI", "kubernetes", "zzz", "india", "co 2", "mlops", "<b>",
             "hostile", "ai engineer", "other", "stated", "engineer", "gurugram", "indiana", "bold", "é", "7*7"]
    terms += [rnd.choice(jobs)["title"][:6] for _ in range(5)] + [rnd.choice(jobs)["company"][:5] for _ in range(3)]
    out = []
    for k in range(n):
        steps = []
        for _ in range(rnd.randint(1, max_steps)):
            r = rnd.random()
            if r < 0.45:
                id_ = rnd.choice(list(opts))
                steps.append({"set": {id_: rnd.choice(opts[id_])}})
            elif r < 0.6:
                steps.append({"set": {"q": rnd.choice(terms)}})
            elif r < 0.93:
                steps.append({"click": rnd.choice(HEADERS)})
            else:
                steps.append({"reset": 1})
        out.append({"name": f"random-{seed}-{k}", "steps": steps})
    return out


def systematic_scenarios(jobs):
    """Every dropdown value alone and then sorted, every header clicked 1-3 times, header pairs."""
    out = []
    for id_, vals in option_values(jobs).items():
        for v in vals:
            out.append({"name": f"{id_}={v!r}", "steps": [{"set": {id_: v}}]})
            out.append({"name": f"{id_}={v!r} then sort Posted x2",
                        "steps": [{"set": {id_: v}}, {"click": "Posted"}, {"click": "Posted"}]})
    for h in HEADERS:
        for times in (1, 2, 3):
            out.append({"name": f"sort {h} x{times}", "steps": [{"click": h}] * times})
    for a, b in zip(HEADERS, HEADERS[1:] + HEADERS[:1]):
        out.append({"name": f"sort {a} then {b}", "steps": [{"click": a}, {"click": b}]})
    out.append({"name": "Schneider sort Posted",
                "steps": [{"set": {"company": "Schneider Electric"}}, {"click": "Posted"}]})
    out.append({"name": "Schneider sort Posted desc",
                "steps": [{"set": {"company": "Schneider Electric"}}, {"click": "Posted"}, {"click": "Posted"}]})
    out.append({"name": "filters then reset",
                "steps": [{"set": {"company": "Citi"}}, {"set": {"q": "pune"}}, {"click": "Role"}, {"reset": 1}]})
    out.append({"name": "search is trimmed and case-insensitive", "steps": [{"set": {"q": "  SrE  "}}]})
    return out


def replay(jobs, scenario):
    """Yield (step, model) after each step of a scenario."""
    m = Model(jobs)
    for st in scenario["steps"]:
        for id_, v in st.get("set", {}).items():
            m.set(id_, v)
        if "click" in st:
            m.click(st["click"])
        if st.get("reset"):
            m.reset()
        yield st, m


# ----------------------------------------------------------------------------- tests
@unittest.skipUnless(CHROME, "Google Chrome / Chromium not found (set CHROME_BIN)")
class HtmlReportFixed(unittest.TestCase):
    """Small hand-made data set: every cell, link and tag is checked, not only the order."""

    @classmethod
    def setUpClass(cls):
        cls.jobs = fixed_jobs()
        cls.scenarios = systematic_scenarios(cls.jobs) + random_scenarios(cls.jobs, 400, seed=1)
        manual = [{"name": "SLB", "platform": "In-house", "url": "https://careers.slb.com", "sector": "energy"},
                  {"name": "Bad <Link>", "platform": "x", "url": "javascript:alert(1)", "sector": ""}]
        cls.result = run_in_chrome(build_page(cls.jobs, manual=manual, failed={"Flaky Co": "HTTP 521"}),
                                   cls.scenarios)

    def test_no_javascript_errors_and_no_injected_code(self):
        r = self.result
        self.assertEqual(r["errors"], [])
        self.assertIsNone(r["pwned"])
        self.assertEqual(r["headInjected"], 0)

    def test_page_furniture(self):
        r = self.result
        self.assertEqual(r["headers"], HEADERS)
        self.assertEqual(r["title"], f"All sectors - devops, sre jobs - {TODAY}")
        self.assertEqual(r["h1"], "All sectors: devops, sre jobs")
        n = len(self.jobs)
        today = sum(1 for j in self.jobs if j["age_days"] == 0)
        new = sum(1 for j in self.jobs if j["first_seen"] == TODAY)
        self.assertTrue(r["sub"].startswith(f"{n} matching jobs, {today} posted today, {new} first seen by the "
                                            f"scraper today ({TODAY})."), r["sub"])
        self.assertEqual(r["todayopt"], f"Today ({today})")
        self.assertFalse(r["sectorHidden"])

    def test_dropdowns_list_every_value_once_and_sorted(self):
        for id_, want in option_values(self.jobs).items():
            with self.subTest(id_):
                self.assertEqual(self.result["options"][id_], want)

    def test_failed_sites_banner_and_manual_links(self):
        r = self.result
        self.assertFalse(r["failed"]["hidden"])
        self.assertIn("Flaky Co", r["failed"]["text"])
        slb, bad = r["manual"]
        self.assertEqual((slb["href"], slb["target"], slb["rel"]),
                         ("https://careers.slb.com", "_blank", "noopener noreferrer"))
        self.assertIn("SLB", slb["text"])
        self.assertIsNone(bad["href"])  # a javascript: URL is not made clickable
        self.assertIn("Bad <Link>", bad["text"])

    def test_every_step_of_every_scenario_matches_the_model(self):
        by_name = {s["name"]: s for s in self.result["scenarios"]}
        checked = 0
        for sc in self.scenarios:
            snaps = by_name[sc["name"]]["snaps"]
            self.assertEqual(len(snaps), len(sc["steps"]))
            for n, ((step, m), snap) in enumerate(zip(replay(self.jobs, sc), snaps)):
                want = m.visible()
                label = f"{sc['name']} step {n + 1} {step}"
                got = snap["rows"]
                self.assertEqual([r["href"] for r in got], [href(self.jobs[i]) for i in want], label)
                self.assertEqual([r["cells"] for r in got], [expected_cells(self.jobs[i]) for i in want], label)
                self.assertEqual(snap["count"], f"{len(want)} of {len(self.jobs)} jobs", label)
                self.assertEqual(snap["none"], not want, label)
                self.assertEqual(snap["sel"], m.f, label)
                for r, i in zip(got, want):
                    j = self.jobs[i]
                    self.assertEqual(r["isNew"], j["first_seen"] == TODAY, label)
                    self.assertEqual(r["injected"], 0, label)
                    tags = [t for t in r["tags"] if t != "tag new"]
                    self.assertEqual(tags[0], f"tag {j['relevance']}", label)
                    self.assertEqual(tags[1:], [f"tag mode-{j['work_mode']}"] if j["work_mode"] else [], label)
                    if r["href"]:
                        self.assertEqual((r["target"], r["rel"]), ("_blank", "noopener noreferrer"), label)
                checked += 1
        self.assertGreater(checked, 800)

    def test_schneider_posted_sort_newest_first_then_oldest_first_undated_last(self):
        by_name = {s["name"]: s["snaps"][-1] for s in self.result["scenarios"]}
        ages = {href(j): j["age_days"] for j in self.jobs}
        for name, rev in (("Schneider sort Posted", False), ("Schneider sort Posted desc", True)):
            shown = [ages[r["href"]] for r in by_name[name]["rows"]]
            dated = [x for x in shown if x is not None]
            self.assertEqual(shown, sorted(dated, reverse=rev) + [None] * (len(shown) - len(dated)), name)
            self.assertIn(None, shown)  # the data really has undated jobs, so "last" is tested

    def test_search_does_not_match_the_not_stated_placeholder(self):
        """'Other / not stated' is only a dropdown label; typing 'stated' must not find jobs without a city."""
        sc = {"name": "stated", "steps": [{"set": {"q": "stated"}}]}
        self.assertEqual(next(replay(self.jobs, sc))[1].visible(), [])
        got = run_in_chrome(build_page(self.jobs), [sc])["scenarios"][0]["snaps"][-1]
        self.assertEqual(got["rows"], [])
        self.assertTrue(got["none"])


@unittest.skipUnless(CHROME, "Google Chrome / Chromium not found (set CHROME_BIN)")
class HtmlReportLarge(unittest.TestCase):
    """6,000 jobs (the size of the real report): random filter / sort sequences, order and count compared."""

    def test_random_sequences_on_a_report_of_6000_jobs(self):
        jobs = random_jobs(6000, seed=7)
        scenarios = random_scenarios(jobs, 80, seed=11, max_steps=5)
        result = run_in_chrome(build_page(jobs), scenarios, compact=True)
        self.assertEqual(result["errors"], [])
        for sc, got in zip(scenarios, result["scenarios"]):
            *_, (_step, m) = list(replay(jobs, sc))
            want = m.visible()
            snap = got["snaps"][-1]
            self.assertEqual(snap["rows"], want, sc["name"])
            self.assertEqual(snap["count"], f"{len(want)} of 6000 jobs", sc["name"])


@unittest.skipUnless(CHROME, "Google Chrome / Chromium not found (set CHROME_BIN)")
class HtmlReportEdgeCases(unittest.TestCase):
    def test_empty_report(self):
        steps = [{"click": "Posted"}, {"set": {"q": "a"}}, {"reset": 1}]
        r = run_in_chrome(build_page([]), [{"name": "x", "steps": steps}])
        self.assertEqual(r["errors"], [])
        for snap in r["scenarios"][0]["snaps"]:
            self.assertEqual((snap["rows"], snap["count"], snap["none"]), ([], "0 of 0 jobs", True))
        self.assertTrue(r["failed"]["hidden"])
        self.assertIn("0 matching jobs", r["sub"])

    def test_report_without_sectors_hides_the_sector_column_and_dropdown(self):
        jobs = [mk(i, "", f"Co {i % 2}", f"Job {i}", "Pune, India", i % 4, "title", "workday", "", ["devops"])
                for i in range(8)]
        scs = [{"name": "s", "steps": [{"click": "Posted"}, {"set": {"company": "Co 1"}}]},
               {"name": "t", "steps": [{"click": "Company"}, {"click": "Company"}]}]
        r = run_in_chrome(build_page(jobs, sector="Fintech"), scs)
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["headers"], HEADERS_NO_SECTOR)
        self.assertTrue(r["sectorHidden"])
        self.assertEqual(r["title"], f"Fintech - devops, sre jobs - {TODAY}")
        for scn, got in zip(scs, r["scenarios"]):
            for (_step, m), snap in zip(replay(jobs, scn), got["snaps"]):
                self.assertEqual([c["cells"] for c in snap["rows"]],
                                 [expected_cells(jobs[i], with_sector=False) for i in m.visible()], scn["name"])

    def test_keywords_and_sector_with_markup_or_placeholders_are_shown_as_text(self):
        jobs = [mk(0, "fintech", "A", "T", "Pune, India", 0, "title", "workday", "", ["x"])]
        page = build_page(jobs, keywords=["<img src=x onerror=alert(1)>", "__DATA__"], sector="<b>S</b>")
        r = run_in_chrome(page, [{"name": "s", "steps": [{"set": {"q": ""}}]}])
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["headInjected"], 0)
        self.assertIn("<img src=x onerror=alert(1)>", r["h1"])
        self.assertIn("__DATA__", r["h1"])
        self.assertEqual(len(r["scenarios"][0]["snaps"][-1]["rows"]), 1)  # the data was not replaced

    def test_job_text_with_template_placeholders_is_kept_as_is(self):
        j = mk(0, "fintech", "__DATA__ Inc", "__TODAY__ __SECTOR__ __LABEL__", "Pune, India", 0, "title",
               "workday", "", ["x"])
        r = run_in_chrome(build_page([j]), [{"name": "s", "steps": [{"set": {"q": "__today__"}}]}])
        row = r["scenarios"][0]["snaps"][-1]["rows"][0]
        self.assertEqual(row["cells"], expected_cells(j))

    def test_reset_clears_search_filters_and_sort(self):
        jobs = fixed_jobs()
        steps = [{"set": {"q": "devops"}}, {"set": {"company": "Citi"}}, {"set": {"age": "7"}}, {"click": "Role"},
                 {"click": "Role"}, {"reset": 1}]
        snap = run_in_chrome(build_page(jobs), [{"name": "r", "steps": steps}])["scenarios"][0]["snaps"][-1]
        self.assertEqual(snap["sel"], {k: "" for k in FILTER_IDS})
        self.assertEqual([x["href"] for x in snap["rows"]], [href(j) for j in jobs])  # report order again
        self.assertEqual(snap["count"], f"{len(jobs)} of {len(jobs)} jobs")


@unittest.skipUnless(CHROME, "Google Chrome / Chromium not found (set CHROME_BIN)")
class FullPipeline(unittest.TestCase):
    """main() with fake fetchers -> the report file the workflow publishes -> checked in the browser."""

    @staticmethod
    def fetcher(c, a):
        if c["name"] == "Down":
            raise RuntimeError("HTTP Error 521")
        rows = [("DevOps Engineer", "Pune, India", 0), ("SRE", "Bengaluru, India (Hybrid)", 3),
                ("Cloud DevOps", "Remote - India", 9), ("Platform SRE", "Chennai, India; Hyderabad, India", None)]
        return [je.make_job(c["name"], "workday", t, loc,
                            "" if age is None else (date.today() - timedelta(days=age)).isoformat(), age,
                            f"https://x/{c['name']}/{i}", i, "devops" if i % 2 == 0 else "sre")
                for i, (t, loc, age) in enumerate(rows)]

    def run_main(self, d):
        cfg = Path(d) / "c.json"
        cfg.write_text(json.dumps({
            "workday": [{"name": "Good", "host": "h", "tenant": "t", "site": "s", "sector": "fintech"},
                        {"name": "Other", "host": "h", "tenant": "t", "site": "s", "sector": "energy"},
                        {"name": "Down", "host": "h", "tenant": "t", "site": "s", "sector": "energy"}],
            "manual": [{"name": "Manual Co", "platform": "x", "url": "https://m.example", "sector": "energy"}]}))
        argv = ["prog", "--config", str(cfg), "--output-dir", str(Path(d) / "out"), "--show-all",
                "--retry-wait", "0", "--keywords", "devops,sre"]
        with mock.patch.object(sys, "argv", argv), mock.patch.dict(je.FETCHERS, {"workday": self.fetcher}), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            je.main("All sectors", str(cfg), str(Path(d) / "out"))
        return (Path(d) / "out" / "latest.html").read_text(encoding="utf-8")

    def test_report_written_by_main_works_in_the_browser(self):
        with tempfile.TemporaryDirectory() as d:
            page = self.run_main(d)
            self.assertEqual(page, next(Path(d, "out").glob("*/report.html")).read_text(encoding="utf-8"))
        steps = [[{"click": "Posted"}], [{"click": "Posted"}, {"click": "Posted"}],
                 [{"set": {"company": "Good"}}, {"click": "Posted"}], [{"set": {"mode": "hybrid"}}],
                 [{"set": {"mode": "remote"}}], [{"set": {"city": "Chennai"}}], [{"set": {"age": "today"}}],
                 [{"set": {"sector": "energy"}}, {"set": {"q": "sre"}}], [{"reset": 1}]]
        r = run_in_chrome(page, [{"name": f"p{i}", "steps": s} for i, s in enumerate(steps)])
        self.assertEqual(r["errors"], [])
        self.assertEqual(r["headers"], HEADERS)
        self.assertIn("Down", r["failed"]["text"])          # the failed site is reported on the page
        self.assertEqual(len(r["manual"]), 1)
        rows = r["scenarios"][-1]["snaps"][-1]["rows"]
        self.assertEqual(len(rows), 8)                      # 2 working sites x 4 jobs
        self.assertEqual(sorted({c["cells"][1] for c in rows}), ["energy", "fintech"])
        dates = [c["cells"][0] for c in r["scenarios"][0]["snaps"][-1]["rows"]]  # newest first, undated last
        self.assertEqual(dates, sorted((x for x in dates if x), reverse=True) + [""] * dates.count(""))
        self.assertTrue(all(c["isNew"] for c in rows))      # first run: everything is new
        hyb = r["scenarios"][3]["snaps"][-1]["rows"]
        self.assertEqual({c["cells"][6] for c in hyb}, {"Hybrid"})

    def test_second_run_marks_nothing_as_new(self):
        with tempfile.TemporaryDirectory() as d:
            self.run_main(d)
            state_file = Path(d, "out", "seen_jobs.json")
            state = json.loads(state_file.read_text())
            for e in state.values():
                e["first_seen"] = "2026-01-01"
            state_file.write_text(json.dumps(state))
            page = self.run_main(d)
        rows = run_in_chrome(page, [{"name": "x", "steps": [{"reset": 1}]}])["scenarios"][0]["snaps"][-1]["rows"]
        self.assertEqual(len(rows), 8)
        self.assertFalse(any(c["isNew"] for c in rows))


class Cities(unittest.TestCase):
    def test_city_names_are_canonical_and_a_job_can_have_several(self):
        a = Namespace(country="India")
        self.assertEqual(je.cities_of("Bangalore, India", a), ["Bengaluru"])
        self.assertEqual(je.cities_of("Pune, India; Chennai, India", a), ["Chennai", "Pune"])
        self.assertEqual(je.cities_of("India", a), [])
        self.assertEqual(je.cities_of("Indiana, USA", a), [])


class JibeDates(unittest.TestCase):
    def test_iso_date_reads_both_formats(self):
        self.assertEqual(je.iso_date("November 24, 2026"), "2026-11-24")
        self.assertEqual(je.iso_date("Nov 4, 2026"), "2026-11-04")
        self.assertEqual(je.iso_date("2026-11-24T07:21:05+0000"), "2026-11-24")
        self.assertEqual(je.iso_date("garbage"), "")
        self.assertEqual(je.iso_date(None), "")

    def test_jibe_job_with_a_text_date_gets_an_iso_date_and_an_age(self):
        row = {"data": {"title": "DevOps", "slug": "1", "req_id": "1", "country_code": "IN",
                        "full_location": "Pune, India", "posted_date": "November 20, 2026",
                        "create_date": "2026-11-21T00:00:00+0000", "description": "x"}}
        a = Namespace(country="India", country_code="IN", keywords=["devops"], max_per_term=100,
                      skills=[], max_age_days=14)
        with mock.patch.object(je, "http_json", return_value={"jobs": [row], "totalCount": 1}):
            jobs = je.fetch_jibe({"name": "Schneider Electric", "host": "careers.se.com"}, a)
        self.assertEqual(jobs[0]["posted"], "2026-11-20")
        self.assertIsNotNone(jobs[0]["age_days"])


if __name__ == "__main__":
    unittest.main()
