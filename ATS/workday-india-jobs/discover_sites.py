#!/usr/bin/env python3
"""Find the current Workday career sites of tenants whose listed site is unreachable.

A Workday tenant publishes its live career sites in https://{tenant}.{wdN}.myworkdayjobs.com/robots.txt
("Sitemap: .../{site}/siteMap.xml"). For every tenant in a workday_failed_sites_*.csv this script
reads that file on the listed data-center instance and on all other known instances (tenants do move),
then writes the discovered sites that answer the Workday jobs API to a CSV (name,url) which
workday_india_jobs.py can scrape:

    python3 discover_sites.py output/workday_failed_sites_<stamp>.csv -o output/discovered_sites.csv
    python3 workday_india_jobs.py --csv output/discovered_sites.csv

Requires: pip install httpx
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from workday_india_jobs import URL_RE, Scraper, Site, india_filter  # noqa: E402

INSTANCES = [f"wd{n}" for n in [*range(1, 13), *range(100, 121), *range(500, 511)]]  # Workday data-center instances
SITEMAP_RE = re.compile(r"^Sitemap:\s*https://[^/]+/([^/\s]+)/siteMap\.xml", re.IGNORECASE | re.MULTILINE)
# some tenants list their sites only as Allow:/Disallow: paths
PATH_RE = re.compile(r"^(?:Allow|Disallow):\s*/([^/\s]+)/\s*$", re.IGNORECASE | re.MULTILINE)
NOT_SITES = {"refreshfacet", "eb", "wday", "static", "assets"}
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


async def robots_sites(client: httpx.AsyncClient, sem: asyncio.Semaphore, tenant: str, inst: str) -> list[str]:
    async with sem:
        try:
            r = await client.get(f"https://{tenant}.{inst}.myworkdayjobs.com/robots.txt")
        except httpx.HTTPError:
            return []
    if r.status_code != 200:
        return []
    names = SITEMAP_RE.findall(r.text)
    if not names:  # no sitemap lines: fall back to the Allow/Disallow paths
        names = [n for n in PATH_RE.findall(r.text) if n.lower() not in NOT_SITES]
    return list(dict.fromkeys(names))


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("failed_csv", help="workday_failed_sites_*.csv from workday_india_jobs.py")
    ap.add_argument("-o", "--out", default="output/discovered_sites.csv")
    ap.add_argument("--new-only", action="store_true",
                    help="extra-sites mode: read robots.txt only on each tenant's listed instance and keep only "
                         "sites that are not already in the input CSV (works with the full workday.csv)")
    args = ap.parse_args()

    tenants: dict[str, dict] = {}  # tenant -> {name, instance, known sites}
    for row in csv.DictReader(open(args.failed_csv, encoding="utf-8")):
        m = URL_RE.match(row["url"])
        if m:
            t = tenants.setdefault(m["tenant"], {"name": row["name"], "inst": m["instance"], "known": set()})
            t["known"].add(m["site"].lower())

    client = httpx.AsyncClient(timeout=20, follow_redirects=False, headers={"User-Agent": UA})
    sem = asyncio.Semaphore(25)
    found: list[tuple[str, str, str, str]] = []  # name, tenant, instance, site

    async def discover(tenant: str, info: dict) -> None:
        insts = [info["inst"]] if args.new_only else INSTANCES
        results = await asyncio.gather(*(robots_sites(client, sem, tenant, i) for i in insts))
        for inst, sites in zip(insts, results):
            for site in sites:
                if args.new_only and site.lower() in info["known"]:
                    continue
                found.append((info["name"], tenant, inst, site))

    await asyncio.gather(*(discover(t, i) for t, i in tenants.items()))
    print(f"{len(tenants)} failed tenants; {len({f[1] for f in found})} publish a robots.txt "
          f"({len(found)} sites listed)", file=sys.stderr)

    # keep only sites that answer the jobs API (the original failing URL may be among them: then it is
    # simply reachable now)
    sc = Scraper(["x"], "title", 25, 30, False)
    ok: list[tuple[str, str, str]] = []

    async def check(name: str, tenant: str, inst: str, site: str) -> None:
        s = Site(name, f"https://{tenant}.{inst}.myworkdayjobs.com/{site}", tenant, inst, site)
        probe, _ = await sc._search(s, {}, "", 0, 1)
        if probe is None:
            return
        india, n = india_filter(probe.get("facets") or [])
        label = f"{name} ({site})" if site.lower() not in tenants[tenant]["known"] else name
        ok.append((label, s.url, f"india={n}" if india else "no-india"))

    await asyncio.gather(*(check(*f) for f in found))
    await sc.close()

    ok.sort()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "url", "note"])
        w.writerows(ok)
    print(f"{len(ok)} reachable sites -> {out}", file=sys.stderr)
    for name, url, note in ok:
        print(f"{note:12} {name} | {url}")


if __name__ == "__main__":
    asyncio.run(main())
