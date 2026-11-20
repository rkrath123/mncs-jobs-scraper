#!/usr/bin/env python3
"""Turn sectors/<sector>/discovery.json into companies.json entries (best site per company, India jobs > 0).
Existing companies.json entries are kept. Review the printed list: a guessed id can belong to another company.

  python3 tools/apply_discovery.py healthcare_pharma [--skip "Name 1,Name 2"]
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ap = argparse.ArgumentParser()
ap.add_argument("sector")
ap.add_argument("--skip", default="")
a = ap.parse_args()
skip = {s.strip().lower() for s in a.skip.split(",") if s.strip()}
d = ROOT / "sectors" / a.sector
disc = json.loads((d / "discovery.json").read_text(encoding="utf-8"))
cand = json.loads((d / "candidates.json").read_text(encoding="utf-8"))["candidates"]
cfg_path = d / "companies.json"
cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {"manual": []}
have = {c["name"].lower() for k, v in cfg.items() for c in v}
added, none = [], []
for c in cand:
    name = c["name"]
    if name.lower() in have or name.lower() in skip:
        continue
    best = max((r for r in disc.get(name, []) if r["india_jobs"] > 0), key=lambda r: r["india_jobs"], default=None)
    if not best:
        none.append(name)
        continue
    cfg.setdefault(best["platform"], []).append(best["config"])
    added.append((name, best["platform"], best["india_jobs"], best["site_total"]))
cfg_path.write_text(json.dumps(cfg, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"== {a.sector}: added {len(added)}")
for n, p, i, t in added:
    print(f"   {n:30} {p:16} India {i:>4} of {t}")
print("   no India jobs found / not discoverable:", ", ".join(none))
