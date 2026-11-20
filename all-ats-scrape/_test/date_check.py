"""Spot-check the report's posting dates against the live job pages, for every ATS present.

Independent of the report's own extraction code: for each sampled dated job the page is fetched again and *every*
date-like token is parsed (ISO, 'Monday, September 28, 2026', '28 Sep 2026', '1/16/26', 'Thu Nov 12 00:00:00 UTC 2026').
Verdicts per job:
  confirmed     a posting-labelled date on the page equals the report date (+-1 day for time zones)
  weak          no labelled date, but the report date appears somewhere on the page
  CONTRADICTED  the page has a posting-labelled date and it differs from the report date by more than a day
  inconclusive  nothing date-like on the page (JS-rendered, blocked, ...)
"""
import collections
import datetime as dt
import json
import random
import re
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

report = sys.argv[1]
PER_ATS = int(sys.argv[2]) if len(sys.argv) > 2 else 4
html = open(report, encoding="utf-8").read()
D = json.loads(re.search(r"const DATA=(.*?);\nconst SNAP", html, re.S).group(1))
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
MON = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def report_date(t):
    return dt.datetime.fromtimestamp(t / 1000, dt.timezone.utc).astimezone(IST).date()


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


def workday_api(url):
    p = urlparse(url); parts = [s for s in p.path.split("/") if s]
    if parts and re.match(r"^[a-z]{2}([-_][A-Za-z]{2})?$", parts[0]) and "job" in parts[2:]: parts = parts[1:]
    i = parts.index("job")
    return f"{p.scheme}://{p.netloc}/wday/cxs/{p.netloc.split('.')[0]}/{parts[i-1]}/{'/'.join(parts[i:])}"


def dates_in(text):
    """[(position, date)] for every date-like token."""
    out = []
    def add(m, y, mo, d):
        try: out.append((m.start(), dt.date(y, mo, d)))
        except ValueError: pass
    for m in re.finditer(r"(?<!\d)(20\d\d)-(\d\d)-(\d\d)(?!\d)", text): add(m, int(m[1]), int(m[2]), int(m[3]))
    for m in re.finditer(r"\b([A-Z][a-z]{2,8})\.? (\d{1,2})(?:st|nd|rd|th)?,? (20\d\d)\b", text):
        if m[1][:3].lower() in MON: add(m, int(m[3]), MON[m[1][:3].lower()], int(m[2]))
    for m in re.finditer(r"\b(\d{1,2})(?:st|nd|rd|th)? ([A-Z][a-z]{2,8})\.?,? (20\d\d)\b", text):
        if m[2][:3].lower() in MON: add(m, int(m[3]), MON[m[2][:3].lower()], int(m[1]))
    for m in re.finditer(r"\b[A-Z][a-z]{2} ([A-Z][a-z]{2}) (\d{1,2}) [\d:]+ \w+ (20\d\d)\b", text):
        if m[1].lower() in MON: add(m, int(m[3]), MON[m[1].lower()], int(m[2]))
    for m in re.finditer(r"(?<![\d/])(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})(?![\d/])", text):
        a, b, y = int(m[1]), int(m[2]), int(m[3]); y += 2000 if y < 100 else 0
        add(m, y, *( (b, a) if a > 12 else (a, b) ))
    return out


LABEL = re.compile(r"date ?posted|posted ?(on|date|at)?|posting (start )?date|published|publish_date|date:|\bdate\b|start ?date|created", re.I)


def check(row):
    ats, url, t = row["ats"], row["url"], row["t"]
    want = report_date(t)
    if ats == "workday":
        try: body = get(workday_api(url)); j = json.loads(body)["jobPostingInfo"]; got = dt.date.fromisoformat(j["startDate"])
        except Exception: return ats, url, "inconclusive", ""
        return ats, url, ("confirmed" if abs((got - want).days) <= 1 else "CONTRADICTED"), f"api={got} report={want}"
    body = get(url)
    toks = dates_in(body)
    if not toks: return ats, url, "inconclusive", ""
    labelled = [d for pos, d in toks if LABEL.search(body[max(0, pos - 60):pos + 5])]
    near = lambda ds: any(abs((d - want).days) <= 1 for d in ds)
    if labelled and near(labelled): return ats, url, "confirmed", f"report={want}"
    if near([d for _, d in toks]): return ats, url, "weak", f"report={want}"
    if labelled: return ats, url, "CONTRADICTED", f"report={want} page_labelled={sorted(set(map(str, labelled)))[:4]}"
    return ats, url, "inconclusive", ""


random.seed(42)
by = collections.defaultdict(list)
for r in D:
    if r["t"] is not None: by[r["ats"]].append(r)
sample = [r for a, rows in by.items() for r in random.sample(rows, min(PER_ATS, len(rows)))]
with ThreadPoolExecutor(8) as pool: res = list(pool.map(check, sample))
tab = collections.defaultdict(collections.Counter)
for ats, url, v, note in res: tab[ats][v] += 1
print(f"{'ATS':20} confirmed weak inconclusive CONTRADICTED   (dated jobs in report)")
for a in sorted(tab):
    c = tab[a]; print(f"{a:20} {c['confirmed']:9} {c['weak']:4} {c['inconclusive']:12} {c['CONTRADICTED']:12}   ({len(by[a])})")
bad = [(a, u, n) for a, u, v, n in res if v == "CONTRADICTED"]
print("\nCONTRADICTED details:"); [print("  ", b) for b in bad] if bad else print("   none")
