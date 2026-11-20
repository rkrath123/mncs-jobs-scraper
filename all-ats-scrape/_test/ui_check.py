"""Compute expected row counts for many filter scenarios from the report's own DATA (independent of the page's JS),
run them through the real page in jsdom, and compare."""
import datetime as dt
import json
import math
import re
import subprocess
import sys

report = sys.argv[1]
html = open(report, encoding="utf-8").read()
D = json.loads(re.search(r"const DATA=(.*?);\nconst SNAP", html, re.S).group(1))
snap = dt.datetime.fromisoformat(re.search(r'new Date\("([^"]+)"\)', html).group(1)).timestamp() * 1000
IST, DAY = 19800000, 86400000


COMPANIES = {y["company"].lower() for y in D}


def expected(a):
    days = a.get("days", "7")
    cut = -math.inf if days == "0" else (math.floor((snap + IST) / DAY) * DAY - IST if days == "today" else snap - int(days) * DAY)
    n = 0
    for x in D:
        if x["t"] is None:
            if not a.get("undated"):
                continue
        elif x["t"] < cut or x["t"] > snap + DAY:
            continue
        if a.get("match") and x["m"] != a["match"]:
            continue
        w = x["w"]; rem = "L" in w or "D" in w
        wm = a.get("work")
        if wm == "remote" and not rem: continue
        if wm == "mention" and not (rem or "M" in w): continue
        if wm == "none" and (rem or "M" in w): continue
        if wm in ("L", "D", "H") and wm not in w: continue
        t = a.get("topic")
        if t and not (t in x["tk"] or t in x["dk"]): continue
        c = a.get("city")
        if c == "__none__":
            if x["city"]: continue
        elif c and c not in x["city"]: continue
        if a.get("ats") and x["ats"] != a["ats"]: continue
        cq = a.get("company", "").strip().lower()
        if cq:
            if cq in COMPANIES:
                if x["company"].lower() != cq: continue
            elif cq not in x["company"].lower(): continue
        q = a.get("q", "").lower()
        if q and q not in (x["title"] + " " + x["company"] + " " + x["loc"]).lower(): continue
        n += 1
    return n


topics = sorted({t for x in D for t in x["tk"] + x["dk"]})
cities = sorted({c for x in D for c in x["city"]}) + ["__none__"]
atss = sorted({x["ats"] for x in D})
S = []
for d in ["today", "1", "2", "3", "7", "30", "0"]:
    for u in (False, True):
        S.append({"name": f"days={d} undated={u}", "actions": {"days": d, "undated": u}})
for m in ["title", "description"]:
    S.append({"name": f"match={m}", "actions": {"days": "0", "undated": True, "match": m}})
for wm in ["remote", "L", "D", "mention", "H", "none"]:
    S.append({"name": f"work={wm}", "actions": {"days": "0", "undated": True, "work": wm}})
for t in topics:
    S.append({"name": f"topic={t}", "actions": {"days": "30", "undated": True, "topic": t}})
for c in cities:
    S.append({"name": f"city={c}", "actions": {"days": "0", "undated": True, "city": c}})
    S.append({"name": f"city={c} 7d", "actions": {"city": c}})
for a in atss:
    S.append({"name": f"ats={a}", "actions": {"days": "0", "undated": True, "ats": a}})
for q in ["bengaluru", "engineer", "zzzznotfound", "Pune"]:
    S.append({"name": f"q={q}", "actions": {"days": "0", "undated": True, "q": q}})
import collections
cnt = collections.Counter(x["company"] for x in D)
names = [c for c, _ in cnt.most_common(15)]
prefix = [c for c in cnt if c and any(o != c and o.lower().startswith(c.lower()) for o in cnt)][:5]  # exact vs substring
for c in names + prefix:
    S.append({"name": f"company={c}", "actions": {"days": "0", "undated": True, "company": c}})
    S.append({"name": f"company upper {c}", "actions": {"days": "0", "undated": True, "company": c.upper() + " "}})
for q in ["tech", "bank", "zzzznotfound"]:
    S.append({"name": f"company~{q}", "actions": {"days": "0", "undated": True, "company": q}})
S.append({"name": "company+city", "actions": {"days": "0", "undated": True, "company": names[0], "city": "Bengaluru"}})
S.append({"name": "combo1", "actions": {"days": "30", "city": "Bengaluru", "topic": topics[0], "work": "remote", "match": "title"}})
S.append({"name": "combo2", "actions": {"days": "7", "city": "Hyderabad", "ats": "greenhouse", "q": "engineer"}})
S.append({"name": "combo3", "actions": {"days": "0", "undated": True, "city": "__none__", "work": "none", "match": "description"}})

for s in S:
    s["expected"] = expected(s["actions"])
json.dump(S, open("all-ats-scrape/_test/scenarios.json", "w"))
r = subprocess.run(["node", "all-ats-scrape/_test/ui_test.js", report, "all-ats-scrape/_test/scenarios.json"], capture_output=True, text=True)
if r.returncode:
    print(r.stderr[-1500:]); sys.exit(1)
actual = json.loads(r.stdout)
bad = [(s["name"], s["expected"], actual[s["name"]]["count"]) for s in S if actual[s["name"]]["count"] != s["expected"]]
print(f"UI scenarios: {len(S)}, mismatches: {len(bad)}")
for b in bad[:15]: print("  MISMATCH", b)
rd = actual["__render"]; print("render checks:", rd)
print("sample:", {k: (v["count"], v["stat"]) for k, v in list(actual.items())[:3]})
