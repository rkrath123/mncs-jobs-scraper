#!/usr/bin/env python3
"""Find which candidates of a sector have a scrapable career site with jobs in India.

For each company in sectors/<sector>/candidates.json it guesses ATS identifiers from the name and checks:
  Workday (robots.txt gives the site names, jobs API gives the India count), Greenhouse, Lever, SmartRecruiters.
Only public JSON endpoints that the career sites themselves use; nothing is bypassed.
Result: sectors/<sector>/discovery.json (+ a table). Review it, then put the verified entries into
sectors/<sector>/companies.json (same format as fintech/energy). Companies that match nothing need a
manual look (custom sites, Oracle / SuccessFactors / Eightfold / Phenom hosts).

  python3 tools/discover.py cybersecurity
  python3 tools/discover.py cybersecurity --only crowdstrike,okta --hint "Okta=okta|okta-inc"
"""
import argparse
import concurrent.futures as cf
import json
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import jobs_engine as E  # noqa: E402

WD_NUMBERS = [1, 5, 3, 12, 2, 10, 103, 501, 502, 105, 102]
STOP = {"inc", "corp", "corporation", "group", "technologies", "technology", "systems", "the", "company",
        "holdings", "international", "ltd", "plc", "networks", "software", "solutions", "semiconductor"}
INDIA_RE = re.compile(r"(?<![a-z])(india|bengaluru|bangalore|hyderabad|pune|mumbai|chennai|gurugram|gurgaon|noida|delhi)(?![a-z])")


def slugs(name, extra):
    base = unicodedata.normalize("NFKD", name.replace("’", "'")).encode("ascii", "ignore").decode().lower()
    words = re.findall(r"[a-z0-9]+", base)
    core = [w for w in words if w not in STOP] or words
    out = []
    for s in (extra + ["".join(core), "".join(words), core[0], core[-1], "-".join(core), "".join(core[:2])] + [w for w in core if len(w) >= 4]):
        if s and s not in out:
            out.append(s)
    return out


def get(url, body=None):
    try:
        return E.http_json(url, body, retries=1, timeout=12)
    except Exception:
        return None


def text(url):
    try:
        return E.http_text(url, retries=1, timeout=12)
    except Exception:
        return None


def india_total_workday(host, tenant, site):
    api = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    probe = get(api, {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    if not probe:
        return None
    facet = E.find_country_facet(probe.get("facets"), "India")
    if facet:
        r = get(api, {"appliedFacets": {facet[0]: [facet[1]]}, "limit": 1, "offset": 0, "searchText": ""})
        return (r or {}).get("total", 0), probe.get("total", 0), True
    r = get(api, {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "India"})
    n = sum(1 for p in (r or {}).get("jobPostings", [])
            if INDIA_RE.search((p.get("locationsText", "") + " " + p.get("externalPath", "")).lower()))
    return n, probe.get("total", 0), False


def try_workday(slug):
    for n in WD_NUMBERS:
        host = f"{slug}.wd{n}.myworkdayjobs.com"
        robots = text(f"https://{host}/robots.txt")
        if not robots:
            continue
        sites = []
        for m in re.finditer(r"(?:Sitemap: https://[^/]+|Allow:|Disallow:)\s*/?([A-Za-z0-9_\-]+)/", robots):
            if m.group(1) not in sites and m.group(1).lower() not in ("refreshfacet", "talentcommunity", "wday", "login"):
                sites.append(m.group(1))
        res = []
        for site in sites[:4]:
            t = india_total_workday(host, slug, site)
            if t and t[1]:
                res.append({"platform": "workday", "config": {"host": host, "tenant": slug, "site": site},
                            "india_jobs": t[0], "site_total": t[1], "facet": t[2]})
        if res:
            return res
    return []


def try_greenhouse(slug):
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    if not d or "jobs" not in d:
        return []
    n = sum(1 for j in d["jobs"] if INDIA_RE.search(((j.get("location") or {}).get("name", "")).lower()))
    if not d["jobs"]:
        return []
    return [{"platform": "greenhouse", "config": {"board": slug}, "india_jobs": n, "site_total": len(d["jobs"])}]


def try_lever(slug):
    d = get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(d, list):
        return []
    n = sum(1 for j in d if INDIA_RE.search(json.dumps(j.get("categories", {})).lower()))
    if not d:
        return []
    return [{"platform": "lever", "config": {"company_id": slug}, "india_jobs": n, "site_total": len(d)}]


def try_smartrecruiters(slug):
    d = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?country=in&limit=1")
    if not d or "totalFound" not in d:
        return []
    allj = get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1") or {}
    if not allj.get("totalFound"):  # SmartRecruiters answers 200 with 0 postings for any unknown id
        return []
    return [{"platform": "smartrecruiters", "config": {"company_id": slug}, "india_jobs": d["totalFound"],
             "site_total": allj["totalFound"]}]


def discover(name, extra):
    found = []
    for slug in slugs(name, extra):
        for fn in (try_workday, try_greenhouse, try_lever, try_smartrecruiters):
            for r in fn(slug):
                r["config"]["name"] = name
                found.append(r)
        if any(r["india_jobs"] for r in found):
            break
    return found


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sector")
    ap.add_argument("--only", default="", help="comma separated name filter")
    ap.add_argument("--hint", action="append", default=[], help='"Company Name=slug1|slug2" extra identifiers')
    ap.add_argument("--workers", type=int, default=24)
    a = ap.parse_args()
    d = ROOT / "sectors" / a.sector
    cands = json.loads((d / "candidates.json").read_text(encoding="utf-8"))["candidates"]
    hints = {k.lower(): v for k, v in json.loads((ROOT / "tools" / "slug_hints.json").read_text()).items()}
    hints.update({h.split("=", 1)[0].strip().lower(): h.split("=", 1)[1].split("|") for h in a.hint})
    only = [o.strip().lower() for o in a.only.split(",") if o.strip()]
    names = [c["name"] for c in cands if not only or any(o in c["name"].lower() for o in only)]
    out_path = d / "discovery.json"
    prev = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    with cf.ThreadPoolExecutor(a.workers) as ex:
        futs = {ex.submit(discover, n, hints.get(n.lower(), [])): n for n in names}
        for f in cf.as_completed(futs):
            prev[futs[f]] = f.result()
    out_path.write_text(json.dumps(dict(sorted(prev.items())), indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{'company':34} {'platform':16} {'India':>6} {'site total':>10}  config")
    for n in sorted(names):
        rs = prev.get(n) or []
        if not rs:
            print(f"{n:34} {'-':16} {'':>6} {'':>10}  (nothing found: manual look needed)")
        for r in rs:
            cfg = {k: v for k, v in r["config"].items() if k != "name"}
            print(f"{n:34} {r['platform']:16} {r['india_jobs']:>6} {r['site_total']:>10}  {json.dumps(cfg)}")


if __name__ == "__main__":
    main()
