#!/usr/bin/env python3
"""Run the job scraper for any sector that has sectors/<sector>/companies.json.

  python3 scrape_sector.py --list                         # sectors, verified companies, candidates left
  python3 scrape_sector.py cybersecurity --keywords "sre,devops"   # India, last 14 days, only new jobs
  python3 scrape_sector.py software_cloud --keywords "java,testing" --show-all --open
  python3 scrape_sector.py semiconductors_hardware --keywords mlops --companies nvidia,amd
(without --keywords you are asked for them)

Every other option is the same as for fintech_scrape_jobs.py (see `--help` after the sector name).
Reports and seen-jobs state go to output/<sector>/.
"""
import json
import sys
from pathlib import Path

from jobs_engine import main

BASE_DIR = Path(__file__).resolve().parent
SECTORS = BASE_DIR / "sectors"


def sector_status():
    rows = []
    for d in sorted(p for p in SECTORS.iterdir() if p.is_dir()):
        cfg, cand = d / "companies.json", d / "candidates.json"
        n = sum(len(v) for k, v in json.loads(cfg.read_text()).items() if k != "manual") if cfg.exists() else 0
        m = len(json.loads(cfg.read_text()).get("manual", [])) if cfg.exists() else 0
        c = len(json.loads(cand.read_text())["candidates"]) if cand.exists() else 0
        tier = json.loads(cand.read_text()).get("tier") if cand.exists() else None
        rows.append((d.name, n, m, c, tier, cfg.exists()))
    return rows


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "--list"):
        print(f"{'sector':28} {'scraped':>8} {'manual':>7} {'to verify':>10} {'tier':>5}  status")
        for name, n, m, c, tier, ready in sector_status():
            print(f"{name:28} {n:>8} {m:>7} {c:>10} {str(tier or '-'):>5}  {'ready' if ready else 'not verified yet'}")
        sys.exit(0)
    sector = sys.argv.pop(1)
    cfg = SECTORS / sector / "companies.json"
    if not cfg.exists():
        sys.exit(f"No {cfg.relative_to(BASE_DIR)} yet. Run `python3 scrape_sector.py --list`.")
    main(sector.replace("_", " ").title(), cfg, BASE_DIR / "output" / sector)
