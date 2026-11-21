"""Independent check of keyword + location decisions.

Inputs: the product's report folder (india_raw.parquet + india_jobs_report.html) and all-ats-scrape/_test/test_raw.parquet
(a superset extract that kept title/description/location).
"""
import ast
import collections
import json
import re
import sys

import duckdb

folder = sys.argv[1]
src = open("all-ats-scrape/jobs_report.py").read()
KW = cities = CITY_NAME = None
for node in ast.parse(src).body:
    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
        n = node.targets[0].id
        if n == "KW" and KW is None: KW = ast.literal_eval(node.value)
        if n == "cities" and cities is None: cities = ast.literal_eval(node.value)
        if n == "CITY_NAME": CITY_NAME = ast.literal_eval(node.value)
LABEL = {"ai_engineer": "ai engineer"}

con = duckdb.connect()
prod = con.execute(f"select * from '{folder}/india_raw.parquet'").fetchdf()
test = con.execute("select * from 'all-ats-scrape/_test/test_raw.parquet'").fetchdf().fillna("")
html = open(f"{folder}/india_jobs_report.html", encoding="utf-8").read()
D = {r["url"]: r for r in json.loads(re.search(r"const DATA=(.*?);\nconst SNAP", html, re.S).group(1))}
# the report row's url is apply_url when there is one; key it by the dataset url so it lines up with `test`
_open = {(a if isinstance(a, str) and a else u): u for a, u in zip(prod.apply_url, prod.url)}
D = {_open[u]: r for u, r in D.items()}
print(f"product rows (raw): {len(prod)}, in report: {len(D)} | test superset rows: {len(test)}")

# ------------------------------------------------------------------ location
print("\n=== LOCATION ===")
def py_in_loc(r):
    l = r.location.lower()
    if r.country_iso == "IN" or re.search(r"\bindia\b", l.replace("little india", "")): return True
    if any(re.search(r"(?<![^\W_])" + c, l) for c in cities): return True
    return (l.strip() == "in" or bool(re.match(r"ind[- ]|in ?- ?(tdc|hyd|blr|remote)", l))
            or r.location.endswith(", in") or bool(re.search(r", in, \d{6}$", l)))
test["py_in_loc"] = test.apply(py_in_loc, axis=1)
print("1. NEW product rule (python re-implementation) vs old test-extract flag differ on:", int((test.py_in_loc != test.in_loc).sum()), "rows")
exp = set(test[test.py_in_loc & test.kw_match].url)
got = set(prod.url)
print(f"2. product urls vs (test: in India & keyword): only-in-product={len(got - exp)} only-in-test={len(exp - got)}")
for u in list(got - exp)[:3]: print("     only in product:", u)
for u in list(exp - got)[:3]: print("     only in test   :", u)
foreign = prod[(prod.country_iso != "") & (prod.country_iso != "IN")]
print(f"3. PRECISION: product rows whose country_iso is set and not IN: {len(foreign)} of {len(prod)}")
for (c, l), n in collections.Counter(zip(foreign.country_iso, foreign.location)).most_common(12): print(f"     {c:3} {l[:70]:70} x{n}")
empty = prod[prod.country_iso == ""]
no_india_word = empty[~empty.location.str.lower().str.contains(r"\bindia\b", regex=True)]
print(f"   rows with no country_iso and no 'india' word (kept only because a city name matched): {len(no_india_word)}")
for l, n in collections.Counter(no_india_word.location).most_common(8): print(f"     {l[:70]:70} x{n}")
cand = test[(~test.py_in_loc) & test.state_loc & test.kw_match]
print(f"4. RECALL: keyword-matching rows NOT in product but location mentions an Indian state/area: {len(cand)}")
cc = collections.Counter(zip(cand.country_iso, cand.location))
for (c, l), n in cc.most_common(25): print(f"     iso={c or '-':3} {l[:70]:70} x{n}")

# ------------------------------------------------------------------ keywords
print("\n=== KEYWORDS ===")
bad_t = bad_d = bad_m = untagged = 0; bad_examples = []
tm = test[test.py_in_loc & test.kw_match & test.url.isin(D)]
for r in tm.itertuples():
    t, d = r.title.lower(), r.description.lower()
    pt = {LABEL.get(k, k) for k, rx in KW.items() if re.search(rx, t)}
    pd_ = {LABEL.get(k, k) for k, rx in KW.items() if re.search(rx, d)} - pt
    row = D[r.url]
    if set(row["tk"]) != pt: bad_t += 1; bad_examples.append(("title", r.url, row["tk"], sorted(pt)))
    if set(row["dk"]) != pd_: bad_d += 1; bad_examples.append(("desc", r.url, row["dk"], sorted(pd_)))
    if row["m"] != ("title" if pt else "description"): bad_m += 1
    if not row["tk"] and not row["dk"]: untagged += 1
print(f"1. report tags vs Python re on title/description ({len(tm)} rows): title-tag mismatches={bad_t}, description-tag mismatches={bad_d}, 'matched on' mismatches={bad_m}, untagged rows={untagged}")
for b in bad_examples[:6]: print("     ", b)
print("   rows tagged title:", sum(1 for r in D.values() if r["tk"]), "| description-only:", sum(1 for r in D.values() if not r["tk"] and r["dk"]))
near_only = test[test.py_in_loc & ~test.kw_match]
print(f"2. RECALL: India rows matching only a near-miss spelling (not in report): {len(near_only)}")
NEAR = r"\bai[- ]?engineer|site[- ]reliability|\bk8\b|\bkube\b|llm[- ]?ops|high[- ]performance[- ]comput|\bai[- ]?ops\b"
snip = collections.Counter()
for r in near_only.itertuples():
    for fld, txt in (("title", r.title), ("desc", r.description)):
        m = re.search(NEAR, txt.lower())
        if m: snip[(fld, m.group(0))] += 1; break
for (f, s), n in snip.most_common(12): print(f"     {f:5} {s!r:35} x{n}")
print("3. sample title matches:")
for r in [r for r in D.values() if r["tk"]][:6]: print("     ", r["tk"], "|", r["title"][:70])
print("   sample description-only matches:")
for r in [r for r in D.values() if not r["tk"]][:4]: print("     ", r["dk"], "|", r["title"][:70])

# ------------------------------------------------------------------ city filter
print("\n=== CITY FILTER ===")
miss_b = []
for u, r in D.items():
    l = r["loc"].lower()
    plain = {CITY_NAME.get(c, c.title()) for c in cities if c in l}   # substring, no word boundary
    if plain != set(r["city"]): miss_b.append((r["loc"], r["city"], sorted(plain)))
print("1. word-boundary city detection differs from plain substring on", len(miss_b), "rows (should be rare)")
for m in miss_b[:6]: print("     ", m)
nocity = collections.Counter(r["loc"] for r in D.values() if not r["city"])
print(f"2. 'No city' rows: {sum(nocity.values())}; most common locations:")
for l, n in nocity.most_common(25): print(f"     {l[:70]:70} x{n}")
print("3. per-city sample (city <- location):")
seen = collections.defaultdict(set)
for r in D.values():
    for c in r["city"]:
        if len(seen[c]) < 2: seen[c].add(r["loc"][:60])
for c in sorted(seen, key=lambda c: -sum(1 for r in D.values() if c in r["city"]))[:10]: print(f"     {c:12} <- {sorted(seen[c])}")
