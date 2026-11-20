"""Extract India jobs matching keywords from the Stapply snapshot, then build the HTML report.

Usage: python3 all-ats-scrape/jobs_report.py [YYYY-MM-DD] [-k "a,b,c"] [--build-only] [--open]
  YYYY-MM-DD        run date / output folder (default: today)
  -k, --keywords "a,b"  comma-separated keywords to search instead of the defaults below. Matched as plain,
                    case-insensitive text in the title or description. Output goes to its own folder
                    all-ats-scrape/output/<date>_<keywords>/ so the default report is not overwritten.
  --build-only      skip the slow extract and rebuild the HTML from an existing folder's india_raw.parquet
                    (pass the same --keywords again if the folder was made with them)
  --open            open the finished report in your default browser
  --skip-page-dates don't look up missing posting dates from each job's own page (Workday, SuccessFactors,
                    recruiterbox, pinpoint, personio); those jobs then show n/a

Output goes to all-ats-scrape/output/<date>/: snapshot.txt, india_raw.parquet, india_jobs_report.html
"""
import argparse
import datetime as dt
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd

ap = argparse.ArgumentParser(usage="python3 all-ats-scrape/jobs_report.py [YYYY-MM-DD] [-k 'a,b,c'] [--build-only] [--open]")
ap.add_argument("run_date", nargs="?", default=dt.date.today().isoformat())
ap.add_argument("-k", "--keywords")
ap.add_argument("--build-only", action="store_true")
ap.add_argument("--open", action="store_true", help="open the report in the default browser when done")
ap.add_argument("--skip-page-dates", action="store_true", help="don't look up missing posting dates from job pages")
cli = ap.parse_args()
BUILD_ONLY = cli.build_only
run_date = cli.run_date
try:
    dt.date.fromisoformat(run_date)
except ValueError:
    ap.error(f"'{run_date}' is not a date; use YYYY-MM-DD")

# keyword -> regex (applied to lower-cased text). Column names <key>_t / <key>_d are derived from the keys.
KW = {
    "devops": r"dev[- ]?ops|devsecops",
    "sre": r"\bsre\b|site reliability",
    "mlops": r"\bml[- ]?ops\b",
    "kubernetes": r"kubernetes|\bk8s\b",
    "llmops": r"\bllm[- ]?ops\b",
    "hpc": r"\bhpc\b|high[- ]performance comput",
    "ai_engineer": r"\bai engineer|\bai/ml engineer",
}
LABEL = {"ai_engineer": "ai engineer"}  # key -> text shown in the report, when different from the key

if cli.keywords is not None:
    terms = list(dict.fromkeys(t.strip().lower() for t in cli.keywords.split(",") if t.strip()))
    if not terms:
        ap.error("--keywords needs at least one non-empty keyword")
    if any("'" in t or '"' in t for t in terms):
        ap.error("--keywords cannot contain quotes")
    # Leading word boundary always; trailing one only for short terms (so "container" still matches "containers").
    KW = {f"k{i}": r"\b" + re.escape(t) + (r"\b" if len(t) <= 4 else "") for i, t in enumerate(terms)}
    LABEL = {f"k{i}": t for i, t in enumerate(terms)}
    run_date += "_" + re.sub(r"\W+", "-", "-".join(terms)).strip("-")[:40]

folder = Path("all-ats-scrape/output") / run_date
raw_path = folder / "india_raw.parquet"
if BUILD_ONLY and not raw_path.exists():
    ap.error(f"--build-only needs {raw_path}, which doesn't exist. Run without --build-only first "
             f"(for a keyword run, pass the same -k as before).")
folder.mkdir(parents=True, exist_ok=True)

# India cities: used by the extract (a job is kept if its location mentions one) and for the report's City filter.
cities = ["bengaluru","bangalore","hyderabad","pune","mumbai","chennai","delhi","gurgaon","gurugram","noida","kolkata","ahmedabad","kochi","coimbatore","thiruvananthapuram","trivandrum","indore","jaipur","chandigarh","mohali","nagpur","vadodara","bhubaneswar","lucknow","mysore","mysuru","visakhapatnam","surat","bhopal","vijayawada","madurai","kozhikode","mangalore","mangaluru","nashik","patna","ranchi","dehradun","faridabad","ghaziabad","thane","gandhinagar","cochin"]
# spelling variants -> one display name; every other city is shown as its own name, capitalised
CITY_NAME = {"bangalore": "Bengaluru", "gurgaon": "Gurugram", "mysore": "Mysuru", "trivandrum": "Thiruvananthapuram", "mangalore": "Mangaluru", "cochin": "Kochi"}
CITY_RX = [(CITY_NAME.get(c, c.title()), re.compile(r"(?<![^\W_])" + re.escape(c))) for c in cities]


def job_cities(location):
    """Display names of every known city mentioned in a job's location text (empty if none, e.g. just 'India')."""
    s = location.lower()
    return list(dict.fromkeys(name for name, rx in CITY_RX if rx.search(s)))


# ---------------------------------------------------------------- 1. extract
if not BUILD_ONLY:
    import duckdb

    manifest_url = "https://storage.stapply.ai/jobs/v1/manifest.json"
    with urllib.request.urlopen(manifest_url, timeout=60) as resp:
        (folder / "snapshot.txt").write_text(json.load(resp)["generated_at"])

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    u = "https://storage.stapply.ai/jobs/v1/all.parquet"
    # In India if: the country code says so, or the text says "India" (but not "Little India, Singapore"), or it names
    # a known city (at the start of a word: "Kağıthane" is not Thane), or it is a bare country code / site code that
    # some ATSs use instead of a place ("IN", "IND-BLR-...", "IN - HYD-...", "Remote, in", "Kochi, IN, 682303").
    loc = " OR ".join([
        "country_iso='IN'",
        "regexp_matches(replace(lower(location), 'little india', ''), '\\bindia\\b')",
        "regexp_matches(lower(location), '(^|[^\\pL])(" + "|".join(cities) + ")')",
        "lower(trim(location)) = 'in'",
        "regexp_matches(lower(location), '^ind[- ]|^in ?- ?(tdc|hyd|blr|remote)')",
        "regexp_matches(location, ', in$')",
        "regexp_matches(lower(location), ', in, [0-9]{6}$')",
    ])

    def flags(col, suffix):
        return ", ".join(f"regexp_matches(lower({col}), '{r}') AS {k}_{suffix}" for k, r in KW.items())

    # Work-mode signals. is_remote is the ATS's own flag; the rest are text signals.
    REMOTE_STRONG = (r"fully remote|100% remote|remote[- ]first|remote[- ]friendly|remote position|remote role|"
                     r"remote work|remote opportunit|work(ing)? remotely|work from home|\bwfh\b|work from anywhere|"
                     r"remote \(|\(remote|remote/|/remote|remote or hybrid|hybrid or remote|can be remote|is remote|be remote")
    MODE = (
        "is_remote, "
        "regexp_matches(lower(location), '\\bremote\\b') AS loc_remote, "
        "regexp_matches(lower(location), 'hybrid') AS loc_hybrid, "
        f"regexp_matches(lower(description), '{REMOTE_STRONG}') AS desc_remote_strong, "
        "regexp_matches(lower(description), '\\bremote(ly)?\\b') AS desc_remote_any, "
        "regexp_matches(lower(description), 'hybrid') AS desc_hybrid"
    )

    any_t = " OR ".join(f"regexp_matches(lower(title), '{r}')" for r in KW.values())
    any_d = " OR ".join(f"regexp_matches(lower(description), '{r}')" for r in KW.values())
    con.execute(f"""
    COPY (
     SELECT ats_type, company, title, location, country_iso, employment_type, department, posted_at, url, apply_url,
            {MODE},
            {flags('title','t')}, {flags('description','d')}
     FROM '{u}'
     WHERE ({loc}) AND (({any_t}) OR ({any_d}))
    ) TO '{raw_path}' (FORMAT parquet)
    """)
    print(con.execute(f"select count(*) from '{raw_path}'").fetchall(), "->", raw_path)
    con.close()

# ---------------------------------------------------------------- 2. build report
# The Stapply dataset has no posting date for some ATSs, but each job's own page does. Look each one up once
# (curl: Workday throttles Python's urllib but not curl) and remember it in all-ats-scrape/posted_dates.json. A job's posting
# date never changes, so after the first run only newly seen jobs are looked up.
# ATSs with no date anywhere (manatal, jobvite, jazzhr, google, ...) stay n/a.
DATES_CACHE = Path("all-ats-scrape/posted_dates.json")
WD_LOCALE = re.compile(r"^[a-z]{2}([-_][A-Za-z]{2})?$")
# ats_type -> how to read the date from the job's page (regexes tried in order, first match wins)
PAGE_DATE_ATS = {
    "workday": None,  # JSON API instead of the page, see workday_api_url
    "successfactors": [r'itemprop="datePosted"\s+content="([^"]+)"', r'"datePosted"\s*:\s*"([^"]+)"',
                       r'Posting Start Date:\s*</span>\s*<span[^>]*>\s*([^<]+?)\s*</span>'],
    "breezy": [r'"datePosted"\s*:\s*"([^"]+)"'],  # dataset date is wrong for breezy (often months too late): override it
    "recruiterbox": [r'"datePosted"\s*:\s*"([^"]+)"'],
    "pinpoint": [r'"datePosted"\s*:\s*"([^"]+)"'],
    "talentbrew": [r'"datePosted"\s*:\s*"([^"]+)"'],
    "ycombinator": [r'"datePosted"\s*:\s*"([^"]+)"'],
    "personio": [r'published_at\\?"\s*:\s*\\?"([^"\\]+)'],
    # Only some Avature sites show a date (e.g. Lenovo, Xerox); others have none and stay n/a.
    "avature": [r'field__label">\s*Date:?\s*</div>\s*<div[^>]*>\s*([^<]+?)\s*</div>'],
}


# ATSs whose dataset date is unreliable: always use the page's date when it has one
OVERRIDE_DATE_ATS = {"breezy"}


def workday_api_url(url):
    """https://<tenant>.wdN.myworkdayjobs.com/[locale/]<site>/job/<path> -> the job's public JSON endpoint."""
    p = urlparse(url)
    parts = [s for s in p.path.split("/") if s]
    if parts and WD_LOCALE.match(parts[0]) and "job" in parts[2:]:
        parts = parts[1:]
    i = parts.index("job")
    return f"{p.scheme}://{p.netloc}/wday/cxs/{p.netloc.split('.')[0]}/{parts[i - 1]}/{'/'.join(parts[i:])}"


def to_iso_date(s):
    """'2026-09-23T11:25:14+01:00' | 'Thu Nov 12 00:00:00 UTC 2026' | '1/16/26' -> '2026-09-23' (None if unreadable)."""
    s = s.strip()
    if m := re.match(r"\d{4}-\d{2}-\d{2}", s):
        return m.group(0)
    if m := re.match(r"[A-Z][a-z]{2} ([A-Z][a-z]{2}) (\d{1,2}) [\d:]+ \w+ (\d{4})$", s):
        return dt.datetime.strptime(" ".join(m.groups()), "%b %d %Y").date().isoformat()
    if m := re.match(r"(?:[A-Za-z]+, )?([A-Za-z]+) (\d{1,2}), (\d{4})$", s):  # 'Monday, September 28, 2026'
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                return dt.datetime.strptime(" ".join(m.groups()), fmt).date().isoformat()
            except ValueError:
                pass
        return None
    if m := re.match(r"(\d{1,2})/(\d{1,2})/(\d{2}|\d{4})$", s):  # month/day/year, as SuccessFactors shows it
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        mo, day = (b, a) if a > 12 else (a, b)  # day/month sites: first number can't be a month
        try:
            return dt.date(y + 2000 if y < 100 else y, mo, day).isoformat()
        except ValueError:
            return None
    return None


def http_get(target):
    """Return (status code, body). curl first; the macOS curl can't make a TLS connection to some sites
    (Avature), so when it gets no response at all (000) fall back to Python's urllib."""
    r = subprocess.run(["curl", "-sL", "-m", "30", "-A", "Mozilla/5.0", "-w", "\n%{http_code}", target],
                       capture_output=True, text=True)
    body, _, code = r.stdout.rpartition("\n")
    if code != "000":
        return code, body
    try:
        req = urllib.request.Request(target, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return str(resp.status), resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        return str(e.code), ""
    except Exception:
        return "000", ""


def lookup_date(job):
    """job = (ats_type, url). Return (cacheable, 'YYYY-MM-DD' or None). Not cacheable = try again next run."""
    ats, url = job
    for attempt in range(3):
        try:
            code, body = http_get(workday_api_url(url) if ats == "workday" else url)
            if code == "200":
                if ats == "workday":
                    return True, json.loads(body).get("jobPostingInfo", {}).get("startDate") or None
                for pat in PAGE_DATE_ATS[ats]:
                    if m := re.search(pat, body):
                        return True, to_iso_date(m.group(1))
                return True, None  # page has no date
            if code in ("404", "410"):
                return True, None  # job was taken down
            if code != "429":
                return False, None  # e.g. 403: the site won't serve us
        except (ValueError, OSError, IndexError):
            return False, None
        time.sleep(5 * (attempt + 1))  # 429: throttled, back off
    return False, None


def fill_posted_dates(d):
    cache = json.loads(DATES_CACHE.read_text()) if DATES_CACHE.exists() else {}
    override = d["ats_type"].isin(OVERRIDE_DATE_ATS)
    undated = d["ats_type"].isin(PAGE_DATE_ATS) & ((d["posted_at"] == "") | override)
    todo = [(a, u) for a, u in zip(d.loc[undated, "ats_type"], d.loc[undated, "url"]) if u not in cache]
    if todo:
        print(f"Looking up posting dates for {len(todo)} jobs from their own pages "
              f"(first run is slow; results are cached) ...", flush=True)
        pool = ThreadPoolExecutor(max_workers=8)
        try:
            for n, ((_, u), (ok, date)) in enumerate(zip(todo, pool.map(lookup_date, todo)), 1):
                if ok:
                    cache[u] = date
                if n % 500 == 0:
                    print(f"  {n}/{len(todo)}", flush=True)
                    DATES_CACHE.write_text(json.dumps(cache))
        finally:  # also on Ctrl-C: stop quickly and keep what we have
            pool.shutdown(wait=False, cancel_futures=True)
            DATES_CACHE.write_text(json.dumps(cache))
    page = d.loc[undated, "url"].map(cache).fillna("")
    d.loc[undated, "posted_at"] = page.where(page != "", d.loc[undated, "posted_at"])


SNAPSHOT = (folder / "snapshot.txt").read_text().strip()  # manifest generated_at
KEYS = list(KW)
d = pd.read_parquet(folder / "india_raw.parquet").fillna("")
d = d.drop_duplicates(subset=["url"]).copy()
if not cli.skip_page_dates:
    fill_posted_dates(d)
# ISO8601: the ATSs mix formats (Z / +00:00 / -04:00 / no timezone / fractional seconds); without it pandas 3
# guesses one format from the first row and turns every row in another format into NaT (shown as n/a).
d["posted_at"] = pd.to_datetime(d["posted_at"], errors="coerce", utc=True, format="ISO8601")
d.loc[d.posted_at.dt.year < 2000, "posted_at"] = pd.NaT

rows = []
for r in d.to_dict("records"):
    tk = [LABEL.get(k, k) for k in KEYS if r[k + "_t"]]
    dk = [LABEL.get(k, k) for k in KEYS if r[k + "_d"] and not r[k + "_t"]]
    listed = str(r["is_remote"]).strip().lower() in ("true", "1", "1.0", "yes") or bool(r["loc_remote"])
    w = []
    if listed: w.append("L")
    if r["desc_remote_strong"]: w.append("D")
    if r["desc_remote_any"] and not listed and not r["desc_remote_strong"]: w.append("M")
    if r["loc_hybrid"] or r["desc_hybrid"]: w.append("H")
    pa = r["posted_at"]
    rows.append({
        "t": int(pa.timestamp() * 1000) if pd.notna(pa) else None,
        "m": "title" if tk else "description",
        "tk": tk, "dk": dk, "w": w,
        "ats": r["ats_type"], "company": r["company"], "title": r["title"],
        "loc": r["location"], "url": r["apply_url"] or r["url"],
        "city": job_cities(r["location"]),
    })
data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")

html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--fg:#e2e8f0;--mut:#94a3b8;--acc:#3b82f6;--bd:#334155}
*{box-sizing:border-box}body{margin:0;font:14px system-ui,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:16px 20px;border-bottom:1px solid var(--bd)}h1{margin:0 0 4px;font-size:18px}
.sub{color:var(--mut);font-size:12px}
.bar{display:flex;flex-wrap:wrap;gap:8px;padding:12px 20px;background:var(--card);border-bottom:1px solid var(--bd);align-items:center}
.chips{display:flex;gap:6px;flex-wrap:wrap}
.chip{padding:6px 12px;border:1px solid var(--bd);border-radius:999px;background:transparent;color:var(--fg);cursor:pointer}
.chip.on{background:var(--acc);border-color:var(--acc);color:#fff}
input,select{background:var(--bg);color:var(--fg);border:1px solid var(--bd);border-radius:6px;padding:6px 8px}
label{color:var(--mut);font-size:12px;display:flex;align-items:center;gap:4px}
.stat{padding:8px 20px;color:var(--mut)}
table{width:100%;border-collapse:collapse}th,td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--bd);vertical-align:top}
th{position:sticky;top:0;background:var(--card);cursor:pointer;user-select:none;white-space:nowrap}
tr:hover td{background:#16213a}a{color:#60a5fa;text-decoration:none}a:hover{text-decoration:underline}
.tag{display:inline-block;padding:1px 7px;border-radius:4px;background:#0b3b6e;font-size:11px;margin:1px}
.mut{color:var(--mut)}.dtag{background:#3a2e0b}.wtag{background:#0b3a2a}
</style></head><body>
<header><h1>__H1__</h1>
<div class="sub">Keywords: __KEYWORDS__, searched in job title and job description. Blue tag = found in title, amber tag = found in description only. Source: Stapply hosted dataset, snapshot <span id="snap"></span>. Dates are the posting date reported by the ATS; many ATSs (Workday, SuccessFactors) don't report one.</div></header>
<div class="bar">
 <div class="chips" id="days"></div>
 <label>Relative to
  <select id="ref"><option value="snap">snapshot time</option><option value="now">my current time</option></select></label>
 <label><input type="checkbox" id="undated"> include undated</label>
 <select id="match"><option value="">Title + description</option><option value="title">Title matches</option><option value="description">Description-only</option></select>
 <select id="work" title="Work mode">
  <option value="">Any work mode</option>
  <option value="remote">Remote: any signal (listed or description)</option>
  <option value="L">Remote: listed (ATS flag / location)</option>
  <option value="D">Remote: stated in description</option>
  <option value="mention">Mentions "remote" anywhere</option>
  <option value="H">Hybrid</option>
  <option value="none">No remote signal (on-site)</option>
 </select>
 <select id="topic"><option value="">All topics</option></select>
 <select id="city" title="City (from the job's location text)"><option value="">All cities</option></select>
 <select id="ats"><option value="">All ATS</option></select>
 <input id="company" list="companies" placeholder="Company (type or pick)" size="22" title="Pick a company, or type part of a name"><datalist id="companies"></datalist>
 <input id="q" placeholder="Search title / company / location" size="32">
 <button class="chip" id="reset">Reset</button>
</div>
<div class="stat" id="stat"></div>
<table><thead><tr>
 <th data-k="t">Posted</th><th data-k="title">Title</th><th data-k="company">Company</th>
 <th data-k="loc">Location</th><th data-k="m">Matched on</th><th>Work mode</th><th data-k="ats">ATS</th></tr></thead>
<tbody id="body"></tbody></table>
<div id="end" class="stat" style="text-align:center"></div>
<script>
const DATA=__DATA__;
const SNAP=new Date("__SNAP__");
const IST=5.5*3600e3, DAY=864e5;
const OPTS=[["Today","today"],["1 day",1],["2 days",2],["3 days",3],["7 days",7],["30 days",30],["All",0]];
let days=7, sortK="t", sortDir=-1;
const $=id=>document.getElementById(id);
$("snap").textContent=SNAP.toISOString().slice(0,16).replace("T"," ")+" UTC";
function ref(){return $("ref").value==="now"?Date.now():SNAP.getTime()}
function cutoff(){
  if(days===0)return -Infinity;
  const r=ref();
  if(days==="today")return Math.floor((r+IST)/DAY)*DAY-IST; // start of IST calendar day
  return r-days*DAY;
}
function fill(sel,vals){[...new Set(vals)].filter(Boolean).sort().forEach(v=>{const o=document.createElement("option");o.value=o.textContent=v;sel.appendChild(o)})}
fill($("topic"),DATA.flatMap(r=>[...r.tk,...r.dk]));
fill($("ats"),DATA.map(r=>r.ats));
const co={};DATA.forEach(r=>co[r.company]=(co[r.company]||0)+1);
Object.keys(co).sort((a,b)=>co[b]-co[a]||a.localeCompare(b)).forEach(c=>{const o=document.createElement("option");
  o.value=c;o.label=c+" ("+co[c]+")";$("companies").appendChild(o)});
const NOCITY="__none__",cc={};DATA.forEach(r=>(r.city.length?r.city:[NOCITY]).forEach(c=>cc[c]=(cc[c]||0)+1));
Object.keys(cc).sort((a,b)=>a===NOCITY?1:b===NOCITY?-1:cc[b]-cc[a]).forEach(c=>{const o=document.createElement("option");
  o.value=c;o.textContent=(c===NOCITY?"No city given (e.g. just India)":c)+" ("+cc[c]+")";$("city").appendChild(o)});
$("days").innerHTML=OPTS.map(([l,v])=>`<button class="chip${v===days?" on":""}" data-v="${v}">${l}</button>`).join("");
$("days").onclick=e=>{const b=e.target.closest("button");if(!b)return;days=b.dataset.v==="today"?"today":+b.dataset.v;
  [...$("days").children].forEach(c=>c.classList.toggle("on",c===b));render()};
let tm;const deb=()=>{clearTimeout(tm);tm=setTimeout(render,200)};
["ref","undated","match","work","topic","city","ats"].forEach(i=>$(i).addEventListener("input",render));
$("q").addEventListener("input",deb);$("company").addEventListener("input",deb);
$("reset").onclick=()=>{days=7;$("q").value="";$("topic").value="";$("city").value="";$("ats").value="";$("company").value="";$("match").value="";$("work").value="";$("undated").checked=false;$("ref").value="snap";
  [...$("days").children].forEach(c=>c.classList.toggle("on",c.dataset.v==="7"));render()};
document.querySelectorAll("th").forEach(th=>th.onclick=()=>{const k=th.dataset.k;sortDir=sortK===k?-sortDir:(k==="t"?-1:1);sortK=k;render()});
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function fmt(t){if(t==null)return'<span class="mut">n/a</span>';
  const n=Math.floor((ref()+IST)/DAY)-Math.floor((t+IST)/DAY),rel=n===0?"today":n===1?"yesterday":n>1?n+" days ago":"";
  return new Date(t+IST).toISOString().slice(0,10)+(rel?` <span class="mut">${rel}</span>`:"")}
const cl={};Object.keys(co).forEach(c=>cl[c.toLowerCase()]=1); // a typed name that is exactly a company = that company only
function render(){
  const c=cutoff(),wm=$("work").value,mt=$("match").value,top=$("topic").value,city=$("city").value,ats=$("ats").value,comp=$("company").value.trim().toLowerCase(),q=$("q").value.toLowerCase(),und=$("undated").checked,r=ref();
  let rows=DATA.filter(x=>{
    if(x.t==null){if(!und)return false}
    else if(x.t<c||x.t>r+DAY)return false;
    if(mt&&x.m!==mt)return false;
    if(wm){const w=x.w,rem=w.includes("L")||w.includes("D");
      if(wm==="remote"&&!rem)return false;
      if(wm==="mention"&&!(rem||w.includes("M")))return false;
      if(wm==="none"&&(rem||w.includes("M")))return false;
      if(["L","D","H"].includes(wm)&&!w.includes(wm))return false}
    if(top&&!(x.tk.includes(top)||x.dk.includes(top)))return false;
    if(city&&!(city===NOCITY?!x.city.length:x.city.includes(city)))return false;
    if(ats&&x.ats!==ats)return false;
    if(comp){const n=x.company.toLowerCase();if(!(comp in cl?n===comp:n.includes(comp)))return false}
    if(q&&!(x.s??=(x.title+" "+x.company+" "+x.loc).toLowerCase()).includes(q))return false;
    return true});
  rows.sort((a,b)=>{let x=a[sortK],y=b[sortK];
    if(sortK==="t"){x=x??-Infinity;y=y??-Infinity;return (x-y)*sortDir}
    return String(x).localeCompare(String(y))*sortDir});
  $("stat").textContent=rows.length+" of "+DATA.length+" jobs"+(und?"":" (dated only)");
  cur=rows;shown=0;$("body").innerHTML="";more();
}
const CHUNK=150;let cur=[],shown=0;
const WL={L:"remote (listed)",D:"remote (description)",M:"mentions remote",H:"hybrid"};
const rowHtml=x=>`<tr><td>${fmt(x.t)}</td>
   <td><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.title)}</a></td><td>${esc(x.company)}</td>
   <td>${esc(x.loc)}</td><td>${x.tk.map(t=>`<span class="tag">title: ${esc(t)}</span>`).join("")}${x.dk.map(t=>`<span class="tag dtag">desc: ${esc(t)}</span>`).join("")}</td><td>${x.w.map(c=>`<span class="tag wtag">${WL[c]}</span>`).join("")}</td><td>${esc(x.ats)}</td></tr>`;
function more(){
  if(shown>=cur.length){$("end").textContent=cur.length?"End of results":"No jobs match these filters";return}
  $("body").insertAdjacentHTML("beforeend",cur.slice(shown,shown+CHUNK).map(rowHtml).join(""));
  shown+=CHUNK;$("end").textContent=Math.min(shown,cur.length)+" of "+cur.length+" shown, scroll for more";
  fillScreen();
}
// The observer only fires when the end marker *enters* view. If it is still in view after more() (e.g. the filter
// changed while scrolled to the bottom), no new event comes, so keep loading until it is out of view.
function fillScreen(){requestAnimationFrame(()=>{if(shown<cur.length&&$("end").getBoundingClientRect().top<innerHeight+600)more()})}
new IntersectionObserver(es=>{if(es[0].isIntersecting)more()},{rootMargin:"600px"}).observe($("end"));
render();
</script></body></html>"""
if cli.keywords is not None:
    title = "India jobs: " + " · ".join(LABEL[k] for k in KEYS)
    h1 = title
else:
    title = "India DevOps / K8s / AI / HPC / MLOps jobs"
    h1 = "India jobs: DevOps · Kubernetes · AI Engineer · HPC · MLOps"
html = (html.replace("__TITLE__", title).replace("__H1__", h1)
        .replace("__KEYWORDS__", ", ".join(LABEL.get(k, k) for k in KEYS))
        .replace("__DATA__", data).replace("__SNAP__", SNAPSHOT))
report_path = folder / "india_jobs_report.html"
report_path.write_text(html, encoding="utf-8")
print("rows", len(rows), "->", report_path)
if cli.open:
    webbrowser.open(report_path.resolve().as_uri())
