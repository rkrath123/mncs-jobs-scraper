#!/usr/bin/env python3
"""Add the verified results of tools/discover_sites.py (discovery2.json) to sectors/<sector>/companies.json.

Per company the proposal with the most India jobs is taken. A proposal is skipped when its host / tenant / board
is already configured for another company (wrong-company guard), or when the identifier does not look like the
company (printed as "check" so you can decide).

  python3 tools/apply_discovery2.py gaming            # dry run: prints what would be added
  python3 tools/apply_discovery2.py gaming --write    # writes companies.json
  python3 tools/apply_discovery2.py --all --write
  python3 tools/apply_discovery2.py gaming --write --accept "Ubisoft,Sega"   # also take the "check" ones named here
"""
import argparse
import json
import re
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STOP = {"inc", "corp", "corporation", "group", "technologies", "technology", "systems", "the", "company",
        "holdings", "international", "ltd", "plc", "co", "and", "of", "limited"}


def words(name):
    base = unicodedata.normalize("NFKD", name.replace("’", "'")).encode("ascii", "ignore").decode().lower()
    w = re.findall(r"[a-z0-9]+", base)
    return [x for x in w if x not in STOP] or w


def ident(platform, cfg):
    return {"workday": lambda c: c["tenant"], "oracle": lambda c: c["host"], "greenhouse": lambda c: c["board"],
            "lever": lambda c: c["company_id"], "smartrecruiters": lambda c: c["company_id"],
            "eightfold": lambda c: c["host"], "phenom": lambda c: c["host"], "sf_rss": lambda c: c["host"],
            "jibe": lambda c: c["host"]}[platform](cfg).lower()


def looks_like(name, platform, cfg):
    ident_s = re.sub(r"[^a-z0-9]", "", ident(platform, cfg))
    ws = words(name)
    joined = "".join(ws)
    return any(len(w) >= 3 and w in ident_s for w in ws) or (len(ident_s) >= 3 and ident_s in joined)


def configured_keys():
    keys = {}
    for f in (ROOT / "sectors").glob("*/companies.json"):
        for plat, items in json.loads(f.read_text(encoding="utf-8")).items():
            for c in items:
                try:
                    keys[(plat, ident(plat, c))] = c["name"]
                except KeyError:
                    pass
    return keys


def run(sector, write, accept):
    d = ROOT / "sectors" / sector
    path = d / "discovery2.json"
    if not path.exists():
        return 0, 0
    disc = json.loads(path.read_text(encoding="utf-8"))
    cfg_path = d / "companies.json"
    comp = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    have_names = {c["name"] for items in comp.values() for c in items}
    keys = configured_keys()
    added = checks = 0
    for name, rec in sorted(disc.items()):
        good = [r for r in rec["results"] if r["india_jobs"] > 0]
        if not good or name in have_names:
            continue
        best = max(good, key=lambda r: r["india_jobs"])
        plat, cfg = best["platform"], best["config"]
        k = (plat, ident(plat, cfg))
        if k in keys:
            print(f"  skip  {name:30} {plat} {k[1]} already used by {keys[k]}")
            continue
        if not looks_like(name, plat, cfg) and name not in accept:
            print(f"  check {name:30} {plat:15} {best['india_jobs']:>4}  {k[1]}   (identifier does not look like the company)")
            checks += 1
            continue
        print(f"  add   {name:30} {plat:15} {best['india_jobs']:>4}  {k[1]}")
        comp.setdefault(plat, []).append(cfg)
        keys[k] = name
        added += 1
    if write and added:
        cfg_path.write_text(json.dumps(comp, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return added, checks


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sector", nargs="?")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--accept", default="", help="comma separated company names to take although the check failed")
    a = ap.parse_args()
    if not a.sector and not a.all:
        ap.error("give a sector or --all")
    accept = {x.strip() for x in a.accept.split(",") if x.strip()}
    sectors = [p.parent.name for p in sorted((ROOT / "sectors").glob("*/discovery2.json"))] if a.all else [a.sector]
    total = 0
    for s in sectors:
        print(f"[{s}]")
        n, c = run(s, a.write, accept)
        total += n
    print(f"\n{total} company(ies) {'added' if a.write else 'would be added (dry run, use --write)'}")


if __name__ == "__main__":
    main()
