#!/usr/bin/env python3
"""Remove companies from sectors/<sector>/companies.json.  python3 tools/prune.py <sector> "Name 1" "Name 2" ..."""
import json
import sys
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "sectors" / sys.argv[1] / "companies.json"
drop = {n.lower() for n in sys.argv[2:]}
cfg = json.loads(p.read_text())
for k, v in cfg.items():
    cfg[k] = [c for c in v if c["name"].lower() not in drop]
cfg = {k: v for k, v in cfg.items() if v or k == "manual"}
p.write_text(json.dumps(cfg, indent=1, ensure_ascii=False) + "\n")
print("removed", sorted(drop))
