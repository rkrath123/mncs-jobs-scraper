#!/usr/bin/env python3
"""Print which job-site platform a career page uses.  python3 tools/sniff.py https://careers.example.com ..."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import jobs_engine as E  # noqa: E402

SIG = {
    "workday": r"[a-z0-9\-]+\.wd\d+\.myworkday(?:jobs|site)\.com(?:/[A-Za-z0-9_\-/]+)?",
    "oracle": r"[a-z0-9\-.]+\.fa\.[a-z0-9\-]+\.oraclecloud\.com(?:/hcmUI/CandidateExperience/\w+/sites/\w+)?",
    "successfactors": r"[a-z0-9\-.]+\.(?:successfactors\.(?:com|eu)|sapsf\.com)|/services/rss/job",
    "eightfold": r"[a-z0-9\-]+\.eightfold\.ai",
    "icims": r"[a-z0-9\-]+\.icims\.com",
    "greenhouse": r"boards\.greenhouse\.io/[a-z0-9\-_]+|job-boards\.greenhouse\.io/[a-z0-9\-_]+|gh_jid",
    "lever": r"jobs\.lever\.co/[a-z0-9\-_]+",
    "ashby": r"jobs\.ashbyhq\.com/[a-z0-9\-_.]+",
    "smartrecruiters": r"(?:careers|jobs)\.smartrecruiters\.com/[A-Za-z0-9\-_]+",
    "taleo": r"[a-z0-9\-]+\.taleo\.net",
    "avature": r"[a-z0-9\-.]+\.avature\.net",
    "phenom": r"phenompeople|phenom\.com|cdn\.phenompeople",
    "jibe": r"jibecdn|/api/jobs\?",
    "radancy": r"talentbrew|radancy|data-ph-at-id",
    "comeet": r"comeet\.(?:co|com)",
    "workable": r"apply\.workable\.com/[a-z0-9\-_]+",
    "brassring": r"brassring\.com",
}

for url in sys.argv[1:]:
    try:
        html = E.http_text(url, retries=1, timeout=15)
    except Exception as e:
        print(f"{url}\n   error: {e}")
        continue
    hits = {}
    for k, rx in SIG.items():
        m = sorted({x.group(0) for x in re.finditer(rx, html, re.I)})
        if m:
            hits[k] = m[:3]
    print(url, "->", hits or "no known platform signature (custom site / JS-rendered)")
