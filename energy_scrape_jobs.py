#!/usr/bin/env python3
"""Energy sector (oil & gas, power, renewables, energy technology): jobs in India at MNC career sites,
for the keywords you give.

  python3 energy_scrape_jobs.py --keywords "sre,devops"       # India, last 14 days, only new jobs
  python3 energy_scrape_jobs.py --keywords "java,testing" --show-all --open
  python3 energy_scrape_jobs.py --keywords mlops --companies shell,abb
(without --keywords you are asked for them)
Run `--help` for all options. Companies: sectors/energy/companies.json. Reports: output/energy/.
"""
from pathlib import Path

from jobs_engine import main

BASE_DIR = Path(__file__).resolve().parent

if __name__ == "__main__":
    main("Energy", BASE_DIR / "sectors" / "energy" / "companies.json", BASE_DIR / "output" / "energy")
