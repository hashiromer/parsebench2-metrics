"""Prove build.py --replace before using it: replace a pipeline with the deliverable it was published from, and the
output must equal the base.

  python check_replace.py <base> <out of build.py --replace <control>:...>

Two differences are expected and allowed: metrics_file_sha256 (a re-scored deliverable is a different file with the
same content) and slot scores in the last binary digit (a replaced pipeline's slots are recomputed as fsum / n; about
a quarter of the published ones differ from every formula in that digit, see the runbook). Anything else fails.
"""
import json
import math
import os
import sys

import pandas as pd

BASE, OUT = sys.argv[1], sys.argv[2]
ALLOWED_KEY = "metrics_file_sha256"
fails, allowed, same, n = [], [], 0, 0


def walk(x, y, path):
    if isinstance(x, dict) and isinstance(y, dict):
        for k in set(x) | set(y):
            walk(x.get(k), y.get(k), f"{path}.{k}")
    elif isinstance(x, list) and isinstance(y, list) and len(x) == len(y):
        for i, (u, v) in enumerate(zip(x, y)):
            walk(u, v, f"{path}[{i}]")
    elif x != y:
        if path.endswith(ALLOWED_KEY):
            allowed.append(path)
        elif isinstance(x, float) and isinstance(y, float) and ".slots" in path and math.isclose(x, y, rel_tol=1e-12):
            allowed.append(f"{path} {x!r} -> {y!r}")
        else:
            fails.append(f"{path}: {x!r:.80} -> {y!r:.80}")


for root, _, files in os.walk(OUT):
    for name in files:
        o = os.path.join(root, name); rel = os.path.relpath(o, OUT); b = os.path.join(BASE, rel); n += 1
        if not os.path.exists(b):
            fails.append(f"{rel}: not in the base"); continue
        if open(o, "rb").read() == open(b, "rb").read():
            same += 1; continue
        if rel.endswith(".json"):
            walk(json.load(open(b)), json.load(open(o)), rel)
        elif rel.endswith(".parquet"):
            x, y = pd.read_parquet(b), pd.read_parquet(o)
            if x.shape != y.shape or list(x.columns) != list(y.columns):
                fails.append(f"{rel}: shape {x.shape} -> {y.shape}"); continue
            for c in x.columns:
                eq = (x[c].isna() & y[c].isna()) | (x[c].astype(object) == y[c].astype(object))
                if not eq.all():
                    (allowed if c == ALLOWED_KEY else fails).append(f"{rel}:{c} ({int((~eq).sum())} rows)")
        else:
            fails.append(f"{rel}: differs")

print(f"{n} files written, {same} byte-identical to the base")
for a in allowed:
    print("  allowed:", a)
for f in fails[:40]:
    print("  FAIL:", f)
print("REPLACE PROOF", "PASS" if not fails else f"FAIL ({len(fails)})")
sys.exit(1 if fails else 0)
