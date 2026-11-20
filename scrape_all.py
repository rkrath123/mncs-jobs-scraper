#!/usr/bin/env python3
"""Run every verified sector in one go and get ONE html report with a Sector filter.

  python3 scrape_all.py --keywords "sre,devops,kubernetes,mlops" --open   # all sectors, India, last 14 days, new jobs
  python3 scrape_all.py --keywords "java,testing" --show-all --open       # everything that matches, new ones marked NEW
  python3 scrape_all.py --keywords sre --sectors cybersecurity,fintech,software_cloud --show-all
  python3 scrape_all.py --keywords sre --skip-sectors energy,fintech
  python3 scrape_all.py --keywords "java,testing" --title-only --show-all --companies google,amazon
(without --keywords you are asked for them)

All sites of the chosen sectors are fetched in one parallel pool. Every other option is the same as
for scrape_sector.py (`python3 scrape_all.py --help-engine`). Output goes to output/all/
(latest.html, <date>/report.html, jobs.csv, jobs.md, seen_jobs.json). The per-sector reports in
output/<sector>/ are not touched.
"""
import argparse
import json
import sys
from pathlib import Path

from jobs_engine import main

BASE_DIR = Path(__file__).resolve().parent
SECTORS = BASE_DIR / "sectors"
OUT = BASE_DIR / "output" / "all"


def ready_sectors():
    return sorted(d.name for d in SECTORS.iterdir() if (d / "companies.json").exists())


def combine(names):
    """Merge sectors/<name>/companies.json files; each entry gets a "sector" tag."""
    merged = {}
    for name in names:
        cfg = json.loads((SECTORS / name / "companies.json").read_text(encoding="utf-8"))
        for kind, items in cfg.items():
            for item in items:
                merged.setdefault(kind, []).append({**item, "sector": name})
    return merged


if __name__ == "__main__":
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--sectors", default="", help="comma separated sector names (default: all that are ready)")
    ap.add_argument("--skip-sectors", default="", help="comma separated sector names to leave out")
    ap.add_argument("--help-engine", action="store_true", help="show the scraper options")
    ap.add_argument("-h", "--help", action="store_true")
    own, rest = ap.parse_known_args()
    if own.help:
        print(__doc__)
        sys.exit(0)
    if own.help_engine:
        rest = ["--help"]

    available = ready_sectors()
    chosen = [s.strip() for s in own.sectors.split(",") if s.strip()] or available
    skip = {s.strip() for s in own.skip_sectors.split(",") if s.strip()}
    unknown = [s for s in chosen + sorted(skip) if s not in available]
    if unknown:
        sys.exit(f"Unknown or not verified sector(s): {', '.join(unknown)}\nReady: {', '.join(available)}")
    chosen = [s for s in chosen if s not in skip]
    if not chosen:
        sys.exit("No sectors selected.")

    OUT.mkdir(parents=True, exist_ok=True)
    combined = OUT / "_combined_companies.json"
    combined.write_text(json.dumps(combine(chosen), indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"Sectors ({len(chosen)}): {', '.join(chosen)}")
    sys.argv = [sys.argv[0]] + rest
    main("All sectors", combined, OUT)
