#!/usr/bin/env python3
"""List every configured company with its ATS and the career-site URL the scraper uses.

  python3 tools/list_ats.py                    # table on screen + ats_sources.csv
  python3 tools/list_ats.py --failed-from run.log   # also marks companies listed in a "Failed (N): a, b" line
"""
import argparse
import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

URL = {
    "workday": lambda c: f"https://{c['host']}/{c['site']}",
    "oracle": lambda c: f"https://{c['host']}/hcmUI/CandidateExperience/en/sites/{c['site']}",
    "greenhouse": lambda c: f"https://boards.greenhouse.io/{c['board']}",
    "lever": lambda c: f"https://jobs.lever.co/{c['company_id']}",
    "smartrecruiters": lambda c: f"https://jobs.smartrecruiters.com/{c['company_id']}",
    "jibe": lambda c: f"https://{c['host']}/jobs",
    "sf_rss": lambda c: f"https://{c['host']}/search/",
    "eightfold": lambda c: f"https://{c['host']}/careers",
    "phenom": lambda c: f"https://{c['host']}/{c.get('region', 'global/en')}/search-results",
    "radancy": lambda c: f"https://{c['host']}/{c['path']}",
    "icims": lambda c: " ; ".join(f"https://{h}/jobs" for h in c["hosts"]),
    "brassring": lambda c: f"https://{c['host']}",
    "manual": lambda c: c["url"],
    "goldman": lambda c: "https://www.goldmansachs.com/careers/",
    "deshaw": lambda c: "https://www.deshaw.com/careers",
    "bofa": lambda c: "https://careers.bankofamerica.com/",
    "amazon": lambda c: "https://www.amazon.jobs/",
    "atlassian": lambda c: "https://www.atlassian.com/company/careers/all-jobs",
    "avature": lambda c: f"{c['base']}/SearchJobs",
    "cornerstone": lambda c: f"https://{c['host']}/ux/ats/careersite/{c['site']}/home?c={c['corp']}",
    "apple": lambda c: "https://jobs.apple.com/en-in/search",
    "google": lambda c: "https://www.google.com/about/careers/applications/jobs/results/?location=India",
    "meta": lambda c: "https://www.metacareers.com/jobs",
    "ibm": lambda c: "https://www.ibm.com/careers/search",
    "sap": lambda c: "https://jobs.sap.com/en/jobs/?country=India",
    "taleo": lambda c: f"https://{c['host']}/careersection/{c.get('section', 'ex')}/jobsearch.ftl",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--failed-from", default="", help="log file containing a 'Failed (N): name, name' line")
    a = ap.parse_args()
    failed = set()
    if a.failed_from:
        m = re.search(r"Failed \(\d+\): (.*)", Path(a.failed_from).read_text(encoding="utf-8"))
        failed = set(m.group(1).split(", ")) if m else set()

    rows = []
    for f in sorted((ROOT / "sectors").glob("*/companies.json")):
        for ats, items in json.loads(f.read_text(encoding="utf-8")).items():
            for c in items:
                status = "" if not a.failed_from else ("FAILED" if c["name"] in failed else "ok")
                rows.append([f.parent.name, c["name"], c.get("platform", ats) if ats == "manual" else ats,
                             URL[ats](c), status])
    rows.sort(key=lambda r: (r[0], r[1].lower()))
    out = ROOT / "ats_sources.csv"
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["sector", "company", "ATS", "career_url", "last_status"])
        w.writerows(rows)
    for r in rows:
        print(f"{r[0]:26} {r[1]:32} {r[2]:16} {r[3]}  {r[4]}")
    print(f"\n{len(rows)} companies -> {out}")


if __name__ == "__main__":
    main()
