#!/usr/bin/env python3
"""Build per-sector candidate lists from data/international_mnc_job_search_directory_india.txt.

Rules live in sectors/policy.json (product-based companies only; consulting / IT services / BPO /
EPC / inspection / CRO / staffing and India-origin entries are excluded and listed with a reason).

Writes:
  sectors/<sector>/candidates.json   companies still to verify (not yet in any sectors/*/companies.json)
  sectors/excluded.json              everything left out, with the reason

  python3 tools/build_candidates.py
"""
import json
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "data" / "international_mnc_job_search_directory_india.txt"
SECTORS = ROOT / "sectors"
POLICY = json.loads((SECTORS / "policy.json").read_text(encoding="utf-8"))


def key(name):
    s = unicodedata.normalize("NFKD", name.replace("’", "'")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9&+.' -]", "", s).strip()


def parse():
    """Return [(section_no, subheading, raw_name)] for sections 1-35."""
    joined = []
    for ln in SRC.read_text(encoding="utf-8").splitlines():
        if ln.startswith("    ") and ln.strip() and joined and joined[-1].startswith("-   "):
            joined[-1] += " " + ln.strip()  # wrapped bullet
        else:
            joined.append(ln)
    out, sec, sub, expect = [], None, "", 1
    for ln in joined:
        m = re.match(r"^(\d{1,2})\. (\S.*)$", ln)
        if m and int(m.group(1)) == 36:
            break
        if m and int(m.group(1)) == expect and (expect > 1 or m.group(2).startswith("Energy")):
            sec, sub, expect = expect, "", expect + 1
        elif sec is None:
            continue
        elif ln.startswith("-   "):
            out.append((sec, sub, ln[4:].strip()))
        elif ln.strip() and not ln.startswith(" ") and not ln.startswith("---"):
            sub = ln.strip()
    return out


def configured_names():
    out = {}
    for f in sorted(SECTORS.glob("*/companies.json")):
        for items in json.loads(f.read_text(encoding="utf-8")).values():
            for c in items:
                out.setdefault(key(c["name"]), f.parent.name)
    return out


def main():
    P = POLICY
    renames = {key(k): v for k, v in P["renames"].items()}
    name_sector = {key(k): v for k, v in P["name_to_sector"].items()}
    exclude = {key(n): why for why, names in P["exclude_names"].items() for n in names}
    configured = configured_names()
    seen, per_sector, excluded, excluded_keys = set(), defaultdict(list), [], set()

    def drop(name, reason, sec):
        if key(name) not in excluded_keys:
            excluded_keys.add(key(name))
            excluded.append({"name": name, "reason": reason, "section": sec})

    for sec, sub, raw in parse():
        note = " ".join(re.findall(r"\(([^)]*)\)", raw)).lower()
        name = re.sub(r"\s*\([^)]*\)", "", raw).strip()
        if any(mk in note for mk in P["india_origin_markers"]):
            drop(name, "india_origin", sec)
            continue
        if str(sec) in P["excluded_sections"]:
            drop(name, P["excluded_sections"][str(sec)], sec)
            continue
        name = renames.get(key(name), name)
        k = key(name)
        if k in exclude:
            drop(name, exclude[k], sec)
            continue
        if k in seen:
            continue
        sector = name_sector.get(k) or P["subheading_to_sector"].get(sub) or P["section_to_sector"].get(str(sec))
        seen.add(k)
        per_sector[sector].append({"name": name, "section": sec, "subgroup": sub})

    for add_key in [k for k in P if k.startswith("additions_")]:
        for sector, names in P[add_key].items():
            if sector.startswith("_"):
                continue
            for name in names:
                if key(name) not in seen:
                    seen.add(key(name))
                    per_sector[sector].append({"name": name, "section": None, "subgroup": add_key[len("additions_"):]})

    summary = {}
    for sector, items in per_sector.items():
        cand = [it for it in items if key(it["name"]) not in configured]
        have = [{**it, "configured_in": configured[key(it["name"])]} for it in items if key(it["name"]) in configured]
        meta = P["sectors"].get(sector, {})
        out_dir = SECTORS / sector
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "candidates.json").write_text(json.dumps(
            {"sector": sector, "title": meta.get("title", sector), "tier": meta.get("tier"), "rule": "product_based_only",
             "candidates": cand, "configured_elsewhere": have}, indent=1, ensure_ascii=False) + "\n",
            encoding="utf-8")
        summary[sector] = (len(cand), len(have), "done" if sector in P["_already_done"] else f"tier {meta['tier']}")
    (SECTORS / "excluded.json").write_text(json.dumps(
        {"rule": "product_based_only",
         "excluded": sorted(excluded, key=lambda e: (e["reason"], e["name"].lower()))},
        indent=1, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"{'sector':28} {'candidates':>10} {'configured':>10}  note")
    for s, (n, h, note) in sorted(summary.items(), key=lambda kv: (kv[1][2], kv[0])):
        print(f"{s:28} {n:>10} {h:>10}  {note}")
    print(f"excluded: {len(excluded)}  (sectors/excluded.json)")


if __name__ == "__main__":
    main()
