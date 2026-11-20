#!/usr/bin/env python3
"""Fintech sector: jobs in India at fintech MNC career sites, for the keywords you give.

  python3 fintech_scrape_jobs.py --keywords "sre,devops,kubernetes"   # India, last 14 days, only new jobs
  python3 fintech_scrape_jobs.py --keywords "java,testing" --show-all --open
  python3 fintech_scrape_jobs.py --keywords mlops --companies citi,visa
(without --keywords you are asked for them)
Run `--help` for all options. Companies: sectors/fintech/companies.json. Reports: output/fintech/.
"""
from pathlib import Path

from jobs_engine import main

BASE_DIR = Path(__file__).resolve().parent

if __name__ == "__main__":
    main("Fintech", BASE_DIR / "sectors" / "fintech" / "companies.json", BASE_DIR / "output" / "fintech")
