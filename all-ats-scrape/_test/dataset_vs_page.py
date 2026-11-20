"""For every ATS whose dates come from the Stapply dataset: compare the dataset's posted_at with the job page's own
schema.org JSON-LD "datePosted" (the posting date the employer publishes), on a sample of jobs per ATS."""
import collections
import datetime as dt
import random
import re
import statistics
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import duckdb

raw = sys.argv[1]
N = int(sys.argv[2]) if len(sys.argv) > 2 else 12
rows = duckdb.connect().execute(f"select ats_type, url, posted_at from '{raw}' where nullif(posted_at,'') is not null").fetchall()
by = collections.defaultdict(list)
for a, u, p in rows: by[a].append((u, p))
random.seed(7)
sample = [(a, u, p) for a, v in by.items() for u, p in random.sample(v, min(N, len(v)))]


def get(url):
    r = subprocess.run(["curl", "-sL", "-m", "30", "-A", "Mozilla/5.0", "-w", "\n%{http_code}", url], capture_output=True, text=True)
    body, _, code = r.stdout.rpartition("\n")
    if code == "000":
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=30) as f:
                return f.read().decode("utf-8", "ignore")
        except Exception:
            return ""
    return body if code == "200" else ""


def day(s):
    return dt.date.fromisoformat(re.match(r"\d{4}-\d{2}-\d{2}", s).group(0))


def one(t):
    a, u, p = t
    m = re.search(r'"datePosted"\s*:\s*"([^"]+)"', get(u))
    if not m: return a, None, u
    try: return a, (day(p) - day(m.group(1))).days, u   # dataset minus page
    except Exception: return a, None, u


with ThreadPoolExecutor(8) as pool: res = list(pool.map(one, sample))
tab = collections.defaultdict(list); ex = collections.defaultdict(list)
for a, diff, u in res:
    if diff is not None:
        tab[a].append(diff)
        if abs(diff) > 1: ex[a].append((diff, u))
print(f"{'ATS':18} {'sampled':>7} {'page has datePosted':>20} {'agree(+-1d)':>12} {'dataset later':>14} {'dataset earlier':>16}   median diff (days)")
for a in sorted(by):
    d = tab.get(a, [])
    n = min(N, len(by[a]))
    if not d:
        print(f"{a:18} {n:7} {'0':>20}"); continue
    agree = sum(1 for x in d if abs(x) <= 1); later = sum(1 for x in d if x > 1); earlier = sum(1 for x in d if x < -1)
    print(f"{a:18} {n:7} {len(d):20} {agree:12} {later:14} {earlier:16}   {statistics.median(d):.0f}")
print("\nexamples of disagreement:")
for a, v in ex.items():
    for diff, u in v[:2]: print(f"  {a:14} dataset-page = {diff:+d} days  {u[:100]}")
