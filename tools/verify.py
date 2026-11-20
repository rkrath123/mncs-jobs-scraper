#!/usr/bin/env python3
"""Check every company in sectors/<sector>/companies.json against its live career site.

Runs the scraper with broad keywords (any engineering-type role, any age) into a throw-away folder and
prints India jobs per company. 0 means the site works but has nothing broad matching in India, or the
config is wrong: look at it before keeping the company.

  python3 tools/verify.py cybersecurity [--companies okta,zscaler]
"""
import collections
import csv
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BROAD = "engineer,developer,manager,analyst,scientist,architect,specialist,lead,consultant,administrator"

if __name__ == "__main__":
    sector, extra = sys.argv[1], sys.argv[2:]
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [sys.executable, str(ROOT / "scrape_sector.py"), sector, "--output-dir", tmp, "--keywords", BROAD,
               "--max-age-days", "3650", "--show-all", "--max-per-term", "100"] + extra
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
        errs = [l for l in r.stderr.splitlines() if l.startswith("[error]")]
        counts = collections.Counter()
        for f in Path(tmp).glob("*/jobs.csv"):
            for row in csv.DictReader(f.open(encoding="utf-8")):
                counts[row["company"]] += 1
    import json
    cfg = json.loads((ROOT / "sectors" / sector / "companies.json").read_text())
    names = [(k, c["name"]) for k, v in cfg.items() if k != "manual" for c in v]
    print(f"{'company':32} {'platform':16} India jobs")
    for k, n in sorted(names, key=lambda x: x[1].lower()):
        print(f"{n:32} {k:16} {counts.get(n, 0)}")
    print(f"\n{len(errs)} error(s)")
    for e in errs:
        print(" ", e[:200])
