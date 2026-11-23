#!/usr/bin/env python3
"""Assemble the GitHub Pages site from output/all/.

  python3 tools/build_site.py <site_dir> [--keep-days 30]

<site_dir> may already hold the previous site (restored from the gh-pages branch). It ends up with:
  index.html              latest report
  reports/<date>/         report.html, jobs.csv, jobs.md of each day (last run of the day wins)
  reports/index.html      list of the kept days
  state/seen_jobs.json    "already seen" state, carried over to the next run
"""
import argparse
import html
import shutil
from datetime import date, timedelta
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "output" / "all"

ap = argparse.ArgumentParser()
ap.add_argument("site_dir")
ap.add_argument("--keep-days", type=int, default=30)
a = ap.parse_args()

site = Path(a.site_dir)
(site / "reports").mkdir(parents=True, exist_ok=True)
(site / "state").mkdir(parents=True, exist_ok=True)

shutil.copy(SRC / "latest.html", site / "index.html")
shutil.copy(SRC / "seen_jobs.json", site / "state" / "seen_jobs.json")

today = date.today().isoformat()
dest = site / "reports" / today
dest.mkdir(parents=True, exist_ok=True)
for name in ("report.html", "jobs.csv", "jobs.md"):
    if (SRC / today / name).exists():
        shutil.copy(SRC / today / name, dest / name)

cutoff = (date.today() - timedelta(days=a.keep_days)).isoformat()
days = []
for d in sorted((p for p in (site / "reports").iterdir() if p.is_dir()), reverse=True):
    if d.name < cutoff:
        shutil.rmtree(d)
    else:
        days.append(d.name)

rows = "\n".join(
    f'<li><a href="{html.escape(d)}/report.html">{html.escape(d)}</a> '
    f'(<a href="{html.escape(d)}/jobs.csv">csv</a>, <a href="{html.escape(d)}/jobs.md">md</a>)</li>'
    for d in days)
(site / "reports" / "index.html").write_text(
    '<!doctype html><meta charset="utf-8"><title>MNC jobs - past reports</title>'
    '<body style="font-family:sans-serif;max-width:640px;margin:2em auto">'
    f'<h1>Past reports</h1><p><a href="../">Latest report</a></p><ul>\n{rows}\n</ul></body>',
    encoding="utf-8")
(site / ".nojekyll").write_text("")
print(f"Site built in {site}: {len(days)} day(s) kept.")
