"""Phase 1 (resumable): probe every site once, append facets to a JSONL file."""
import asyncio, json, os, sys
sys.path.insert(0, ".")
import workday_india_jobs as w

OUT = os.path.expanduser("~/.cache/mncs-jobs-scraper/audit_probes.jsonl")


async def main():
    seen = set()
    if os.path.exists(OUT):
        for line in open(OUT):
            try:
                seen.add(json.loads(line)["key"])
            except ValueError:
                pass
    sites = [s for s in w.load_sites("workday.csv") if f"{s.tenant}.{s.instance}/{s.site}" not in seen]
    print(f"{len(seen)} already probed, {len(sites)} to go", flush=True)
    sc = w.Scraper(["x"], "title", 10, 30.0, False, rate=8)
    q = asyncio.Queue()
    for s in sites:
        q.put_nowait(s)
    fh = open(OUT, "a")
    done = 0

    async def worker():
        nonlocal done
        while True:
            try:
                s = q.get_nowait()
            except asyncio.QueueEmpty:
                return
            probe, reason = await sc._search(s, {}, "", 0, 1)
            key = f"{s.tenant}.{s.instance}/{s.site}"
            if probe is None:
                rec = {"key": key, "name": s.name, "err": reason}
            else:
                facets = probe.get("facets") or []
                vals = [(p, l, str(v.get("descriptor") or ""), int(v.get("count") or 0))
                        for p, l, v in w.walk_facets(facets)]
                applied, n = w.india_filter(facets)
                rec = {"key": key, "name": s.name, "api": s.api, "total": probe.get("total"),
                       "india": bool(applied), "india_n": n, "vals": vals,
                       "params": sorted({f.get("facetParameter") or f.get("descriptor") or ""
                                         for f in facets if isinstance(f, dict)})}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            done += 1
            if done % 250 == 0:
                print(done, "http429", sc.stats["http_429"], flush=True)

    await asyncio.gather(*(worker() for _ in range(10)))
    await sc.close()
    print("finished", done, "http429", sc.stats["http_429"], flush=True)

asyncio.run(main())
