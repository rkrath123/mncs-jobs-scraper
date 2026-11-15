#!/usr/bin/env python3
"""Second-stage discovery: find career sites of candidates that tools/discover.py could not place.

discover.py only guesses Workday / Greenhouse / Lever / SmartRecruiters ids. This tool guesses the career-site
HOSTS of a company (careers.<name>.com, jobs.<name>.com, <name>.com/careers ...), keeps those that resolve in DNS,
and then looks at what is behind them:

  * SuccessFactors (jobs2web) RSS     -> /services/rss/job/
  * Jibe                              -> /api/jobs
  * Oracle HCM / Eightfold / Phenom / Workday / Greenhouse / Lever / SmartRecruiters found in the page
  * Taleo / Avature / Ashby / Workable / iCIMS ... are only reported ("unsupported"), nothing is scraped from them

Every config it proposes is tested with the real scraper functions (broad keyword, India only). Result goes to
sectors/<sector>/discovery2.json; tools/apply_discovery2.py puts the verified ones into companies.json.

  python3 tools/discover_sites.py gaming
  python3 tools/discover_sites.py --all                  # every sector, all candidates not configured yet
  python3 tools/discover_sites.py software_cloud --only snowflake,uber
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import socket
import sys
import threading
import time
import unicodedata
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import jobs_engine as E  # noqa: E402

STOP = {"inc", "corp", "corporation", "group", "technologies", "technology", "systems", "the", "company",
        "holdings", "international", "ltd", "plc", "co", "and", "of", "limited", "sa", "ag", "nv", "se"}
ARGS = argparse.Namespace(keywords=["engineer"], country="India", country_code="IN", max_per_term=100,
                          max_age_days=3650, workers=4)
SIG = {
    "workday": r"([a-z0-9\-]{1,60})\.(wd\d{1,4})\.myworkday(?:jobs|site)\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_\-]+)",
    "oracle": r"([a-z0-9\-.]{1,60}\.fa\.[a-z0-9\-.]{0,40}oraclecloud\.com)/hcmUI/CandidateExperience/\w+/sites/(\w+)",
    "eightfold": r"([a-z0-9\-]{1,60}\.eightfold\.ai)",
    "greenhouse": r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9\-_]+)",
    "lever": r"jobs\.lever\.co/([a-z0-9\-_]+)",
    "smartrecruiters": r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9\-_]+)",
}
UNSUPPORTED = {"taleo": r"[a-z0-9\-]{1,60}\.taleo\.net", "avature": r"[a-z0-9\-.]{1,60}\.avature\.net",
               "ashby": r"jobs\.ashbyhq\.com/[a-z0-9\-_.]+", "workable": r"apply\.workable\.com/[a-z0-9\-_]+",
               "icims": r"[a-z0-9\-]{1,60}\.icims\.com", "brassring": r"brassring\.com",
               "successfactors-page": r"career\d*\.successfactors\.(?:com|eu)/career\?company=[A-Za-z0-9_\-]+"}


def words_of(name):
    base = unicodedata.normalize("NFKD", name.replace("’", "'")).encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z0-9]+", base)


def host_slugs(name, extra):
    words = words_of(name)
    core = [w for w in words if w not in STOP] or words
    out = list(extra)
    for s in ("".join(core), "-".join(core), core[0], "".join(words)):
        if s and s not in out:
            out.append(s)
    return [s for s in out if len(s) >= 4 or s in extra]


DNS_SLOTS = threading.Semaphore(10)  # too many parallel lookups make the macOS resolver fail every one of them
_DNS_CACHE = {}


def _lookup(host):
    """One getaddrinfo in a daemon thread (it can hang for minutes on macOS). -> 'ok' | 'nx' | 'retry'."""
    box = []

    def work():
        try:
            socket.getaddrinfo(host, 443)
            box.append("ok")
        except socket.gaierror as e:
            box.append("nx" if e.errno in (socket.EAI_NONAME, getattr(socket, "EAI_NODATA", -999)) else "retry")
        except OSError:
            box.append("retry")

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(3)
    return box[0] if box else "retry"


def resolves(host):
    if host in _DNS_CACHE:
        return _DNS_CACHE[host]
    ok = False
    for attempt in range(2):  # a real "no such name" answer is final; a timeout gets one more try
        with DNS_SLOTS:
            r = _lookup(host)
        if r == "ok":
            ok = True
        if r != "retry":
            break
        time.sleep(0.5)
    _DNS_CACHE[host] = ok
    return ok


T = 6  # seconds per request
BUDGET = 90  # seconds per company


def get_text(url, timeout=T):
    try:
        return E.http_text(url, retries=1, timeout=timeout)
    except Exception:
        return None


def candidate_hosts(name, extra):
    hosts = []
    for s in host_slugs(name, extra):
        for pat in ("careers.{}.com", "jobs.{}.com", "www.{}.com", "{}.com"):
            h = pat.format(s)
            if h not in hosts:
                hosts.append(h)
    return hosts


def deadline(fn, secs, *args):
    """Run fn(*args) in a daemon thread; return its result, or None when it takes longer than `secs`."""
    box = []
    t = threading.Thread(target=lambda: box.append(fn(*args)), daemon=True)
    t.start()
    t.join(secs)
    return box[0] if box else None


def _test(platform, cfg):
    """Run the real fetcher once; return the number of India jobs or None when it errors."""
    try:
        return len(E.FETCHERS[platform](cfg, ARGS))
    except Exception:
        return None


MARK = {"workday": "myworkday", "oracle": "oraclecloud", "greenhouse": "greenhouse.io", "lever": "jobs.lever.co",
        "smartrecruiters": "smartrecruiters.com", "eightfold": "eightfold.ai"}


def finditer(kind, blob):
    """Run SIG[kind] only when its literal marker is in the page (keeps regex time linear on huge pages)."""
    return re.finditer(SIG[kind], blob) if MARK[kind] in blob else iter(())


def probe_host(name, host):
    """Everything we can learn about one career host. Returns (proposals, unsupported platforms seen)."""
    found = []
    rss_url = (f"https://{host}/services/rss/job/?locale=en_GB&keywords=" +
               urllib.parse.quote("(engineer) AND locationSearch:(India)", safe="():"))

    def jibe():
        try:
            j = E.http_json(f"https://{host}/api/jobs?keywords=engineer&location=India&limit=1", retries=1, timeout=T)
            return isinstance(j, dict) and "totalCount" in j
        except Exception:
            return False

    career_host = host.startswith(("careers.", "jobs."))  # RSS / Jibe only live on dedicated career hosts
    with cf.ThreadPoolExecutor(5) as ex:
        f_rss = ex.submit(get_text, rss_url, T) if career_host else None
        f_jibe = ex.submit(jibe) if career_host else None
        f_pages = [ex.submit(get_text, f"https://{host}{p}", T)
                   for p in (("",) if career_host else ("", "/careers", "/jobs"))]
        rss, is_jibe = (f_rss.result(), f_jibe.result()) if career_host else (None, False)
        blob = "\n".join(h[:600_000] for h in (f.result() for f in f_pages) if h)
    if rss and "<rss" in rss[:600]:
        found.append(("sf_rss", {"name": name, "host": host}))
    if is_jibe:
        found.append(("jibe", {"name": name, "host": host}))
    for m in finditer("workday", blob):
        tenant, wd, site = m.group(1), m.group(2), m.group(3)
        if site.lower() in ("wday", "login", "assets"):
            continue
        found.append(("workday", {"name": name, "host": f"{tenant}.{wd}.myworkdayjobs.com", "tenant": tenant, "site": site}))
    for m in finditer("oracle", blob):
        found.append(("oracle", {"name": name, "host": m.group(1), "site": m.group(2)}))
    for m in finditer("greenhouse", blob):
        found.append(("greenhouse", {"name": name, "board": m.group(1)}))
    for m in finditer("lever", blob):
        found.append(("lever", {"name": name, "company_id": m.group(1)}))
    for m in finditer("smartrecruiters", blob):
        found.append(("smartrecruiters", {"name": name, "company_id": m.group(1)}))
    for m in finditer("eightfold", blob):
        ef = m.group(1)
        if ef in ("app.eightfold.ai", "vs-errors.eightfold.ai"):
            continue
        dom = re.search(r"domain=([a-z0-9.\-]+)", blob)
        domains = ([dom.group(1)] if dom else []) + [re.sub(r"^(careers|jobs|www)\.", "", host)]
        for d in dict.fromkeys(domains):
            found.append(("eightfold", {"name": name, "host": ef, "domain": d, "api": "pcsx"}))
            found.append(("eightfold", {"name": name, "host": ef, "domain": d}))
    if re.search(r"phenompeople|cdn\.phenom", blob):
        pid = re.search(r'"pageId"\s*:\s*"(page\d+)"', blob)
        loc = re.search(r'"locale"\s*:\s*"([a-z]{2}_[a-zA-Z]{2,})"', blob)
        reg = re.search(r'href="/([a-z\-]+/[a-z]{2})(?:/|")', blob)
        if pid:
            found.append(("phenom", {"name": name, "host": host, "lang": loc.group(1) if loc else "en_us",
                                     "page_id": pid.group(1), "region": reg.group(1) if reg else "global/en"}))
    low = blob.lower()
    unsupported = sorted({k for k, rx in UNSUPPORTED.items()
                          if k.split("-")[0] in low and re.search(rx, blob, re.I)})
    seen, uniq = set(), []
    for p, c in found:
        k = (p, json.dumps(c, sort_keys=True))
        if k not in seen:
            seen.add(k)
            uniq.append((p, c))
    return uniq[:8], unsupported


def discover_one(name, extra):
    guesses = candidate_hosts(name, extra)
    with cf.ThreadPoolExecutor(12) as ex:
        ok = list(ex.map(resolves, guesses))
    hosts = [h for h, r in zip(guesses, ok) if r]
    results, unsupported, tried, seen_cfg = [], set(), [], set()
    t0 = time.time()
    for host in hosts[:5]:
        if time.time() - t0 > BUDGET:
            break
        got = deadline(probe_host, 30, name, host)
        props, unsup = got if got else ([], [])
        tried.append(host)
        unsupported.update(unsup)
        for p, c in props[:5]:
            key = (p, json.dumps(c, sort_keys=True))
            if key in seen_cfg or time.time() - t0 > BUDGET:
                continue
            seen_cfg.add(key)
            n = deadline(_test, 30, p, c)
            if n is not None:
                results.append({"platform": p, "config": c, "india_jobs": n, "found_on": host})
        if any(r["india_jobs"] for r in results):
            break
    return {"hosts": tried, "results": results, "unsupported": sorted(unsupported)}


def run_sector(sector, only, hints, workers, redo=False):
    d = ROOT / "sectors" / sector
    cands = json.loads((d / "candidates.json").read_text(encoding="utf-8"))["candidates"]
    out_path = d / "discovery2.json"
    prev = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    names = [c["name"] for c in cands if (not only or any(o in c["name"].lower() for o in only))
             and (redo or only or c["name"] not in prev)]
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(discover_one, n, hints.get(n.lower(), [])): n for n in names}
        for f in cf.as_completed(futs):
            try:
                prev[futs[f]] = f.result()
            except Exception as e:  # keep going; record the failure
                prev[futs[f]] = {"hosts": [], "results": [], "unsupported": [], "error": str(e)}
            r = prev[futs[f]]
            print(f"  [{sector}] {futs[f]}: hosts={len(r['hosts'])} best="
                  f"{max([x['india_jobs'] for x in r['results']] or [0])}", file=sys.stderr, flush=True)
    out_path.write_text(json.dumps(dict(sorted(prev.items())), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    hit = [(n, r) for n in names for r in prev[n]["results"] if r["india_jobs"]]
    unsup = sum(1 for n in names if prev[n]["unsupported"] and not any(r["india_jobs"] for r in prev[n]["results"]))
    print(f"{sector}: {len(names)} candidates, {len({n for n, _ in hit})} with India jobs, {unsup} on an unsupported ATS")
    for n, r in hit:
        print(f"  {n:34} {r['platform']:16} {r['india_jobs']:>4}  {r['found_on']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sector", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--redo", action="store_true", help="also re-check companies already in discovery2.json")
    a = ap.parse_args()
    if not a.sector and not a.all:
        ap.error("give a sector or --all")
    hints = {k.lower(): v for k, v in json.loads((ROOT / "tools" / "slug_hints.json").read_text()).items()}
    only = [o.strip().lower() for o in a.only.split(",") if o.strip()]
    sectors = ([p.parent.name for p in sorted((ROOT / "sectors").glob("*/candidates.json"))]
               if a.all else [a.sector])
    for s in sectors:
        run_sector(s, only, hints, a.workers, a.redo)
    sys.stdout.flush()
    os._exit(0)  # leave without waiting for threads stuck on slow servers


if __name__ == "__main__":
    main()
