"""Test-only extract: a superset of the product's extract, with title/description/location kept,
so the product's keyword + location decisions can be re-checked independently in Python."""
import ast
import duckdb

src = open("all-ats-scrape/jobs_report.py").read()
tree = ast.parse(src)
KW = cities = None
for node in tree.body:
    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
        n = node.targets[0].id
        if n == "KW" and KW is None:
            KW = ast.literal_eval(node.value)
        if n == "cities" and cities is None:
            cities = ast.literal_eval(node.value)
assert KW and cities

STATES = ["karnataka", "maharashtra", "telangana", "tamil nadu", "tamilnadu", "kerala", "andhra pradesh", "uttar pradesh",
          "haryana", "gujarat", "rajasthan", "west bengal", "odisha", "orissa", "madhya pradesh", "punjab", "bihar",
          "jharkhand", "uttarakhand", "goa", "assam", "chhattisgarh", "himachal", "jammu", "pondicherry", "puducherry",
          "ncr", "new delhi", "navi mumbai", "greater noida"]
# spelling near-misses of the product's keywords, to see if the product's regexes are too strict
NEAR = r"\bai[- ]?engineer|site[- ]reliability|\bk8\b|\bkube\b|llm[- ]?ops|high[- ]performance[- ]comput|\bai[- ]?ops\b"

loc = "country_iso='IN' OR regexp_matches(lower(location), '\\bindia\\b') OR " + " OR ".join(
    f"lower(location) LIKE '%{c}%'" for c in cities)
state = " OR ".join(f"lower(location) LIKE '%{s}%'" for s in STATES) + " OR regexp_matches(lower(location), '(^|[ ,-])(in|ind)($|[ ,-])')"
any_t = " OR ".join(f"regexp_matches(lower(title), '{r}')" for r in KW.values())
any_d = " OR ".join(f"regexp_matches(lower(description), '{r}')" for r in KW.values())
near = f"regexp_matches(lower(title), '{NEAR}') OR regexp_matches(lower(description), '{NEAR}')"

con = duckdb.connect()
con.execute("INSTALL httpfs; LOAD httpfs; SET enable_progress_bar=false")
con.execute(f"""
COPY (
 SELECT ats_type, url, title, location, country_iso, description,
        ({loc}) AS in_loc, ({state}) AS state_loc, ({any_t}) OR ({any_d}) AS kw_match
 FROM 'https://storage.stapply.ai/jobs/v1/all.parquet'
 WHERE (({loc}) OR ({state})) AND (({any_t}) OR ({any_d}) OR ({near}))
) TO 'all-ats-scrape/_test/test_raw.parquet' (FORMAT parquet)
""")
print(con.execute("select count(*), sum(in_loc::int), sum(kw_match::int) from 'all-ats-scrape/_test/test_raw.parquet'").fetchall())
