"""Phase 2: free-text 'India' (+ top cities for filterless sites) on every dropped site; look at locationsText."""
import asyncio, json, os, sys
sys.path.insert(0, ".")
import workday_india_jobs as w
C = os.path.expanduser("~/.cache/mncs-jobs-scraper/")
recs = {}
for l in open(C + "audit_probes.jsonl"):
    r = json.loads(l); recs[r["key"]] = r
cand = json.load(open(C + "audit_cand.json")); noplace = set(cand["noplace"])
targets = [r for r in recs.values() if "err" not in r and not r["india"] and r["total"]]
OUT = C + "phase2.jsonl"
seen = set()
if os.path.exists(OUT):
    seen = {json.loads(l)["key"] for l in open(OUT)}
targets = [r for r in targets if r["key"] not in seen]
CITIES = ["Bangalore", "Bengaluru", "Hyderabad", "Pune", "Chennai", "Mumbai", "Gurgaon", "Noida", "Delhi"]
print(len(targets), "to check", flush=True)

async def main():
    sc = w.Scraper(["x"], "title", 10, 30.0, False, rate=8)
    q = asyncio.Queue()
    for r in targets: q.put_nowait(r)
    fh = open(OUT, "a"); n = 0
    async def worker():
        nonlocal n
        while True:
            try: r = q.get_nowait()
            except asyncio.QueueEmpty: return
            texts = ["India"] + (CITIES if r["key"] in noplace else [])
            found = []; fail = ""
            for t in texts:
                d, why = await search(sc, r["api"], t)
                if d is None: fail = why; continue
                for p in d.get("jobPostings") or []:
                    loc = p.get("locationsText") or ""
                    if w.INDIA_LOCATION_RE.search(loc):
                        found.append([t, p.get("title"), loc, p.get("externalPath")])
            fh.write(json.dumps({"key": r["key"], "name": r["name"], "noplace": r["key"] in noplace, "found": found, "fail": fail}) + "\n"); fh.flush()
            n += 1
            if n % 250 == 0: print(n, "429s", sc.stats["http_429"], flush=True)
    async def search(sc, api, t):
        body = {"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": t}
        resp, reason = await sc._request("POST", f"{api}/jobs", json=body)
        if resp is None: return None, reason
        try: return resp.json(), ""
        except ValueError: return None, "invalid JSON"
    await asyncio.gather(*(worker() for _ in range(10)))
    await sc.close(); print("finished", n, flush=True)
asyncio.run(main())
