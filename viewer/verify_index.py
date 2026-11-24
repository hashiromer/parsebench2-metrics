"""Rebuild index/*.json from the base's own tables and compare with the published files."""
import json
import math
import sys

import pandas as pd

import index_files as I
from aggregates import DIMS

BASE = sys.argv[1]
pds = pd.read_parquet(f"{BASE}/metrics/page_dimension_scores.parquet")
bp = pd.read_parquet(f"{BASE}/pipelines.parquet")
site = json.load(open(f"{BASE}/index/site.json"))
sp = {p["pipeline"]: p for p in site["pipelines"]}
order = list(bp.pipeline)
fails = 0
stats = {"exact": 0, "close": 0}


def same(a, b, path=""):
    if isinstance(a, float) and isinstance(b, float):
        if a == b: stats["exact"] += 1; return None
        if math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12): stats["close"] += 1; return None
        return f"{path}: {a!r} != {b!r}"
    if isinstance(a, dict) and isinstance(b, dict):
        if list(a) != list(b): return f"{path}: keys {list(a)} != {list(b)}"
        for k in a:
            r = same(a[k], b[k], f"{path}.{k}")
            if r: return r
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b): return f"{path}: len {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            r = same(x, y, f"{path}[{i}]")
            if r: return r
        return None
    return None if a == b and type(a) == type(b) else f"{path}: {a!r} ({type(a).__name__}) != {b!r} ({type(b).__name__})"


def check(name, gen, path):
    global fails
    base = json.load(open(f"{BASE}/{path}"))
    r = same(gen, base, name)
    if r: fails += 1; print("MISMATCH", r[:300])
    return r is None


mats = {}
for d in DIMS:
    slot_rows = sp[order[0]]["slots"][d]["slots"]
    gen, mats[d] = I.dimension_file(d, pds, order, slot_rows)
    print(d, "dimension ok" if check(f"dim/{d}", gen, f"index/dimensions/{d}.json") else "")
print("pairwise ok" if check("pairwise", I.pairwise(order, mats), "index/pairwise.json") else "")
nok = 0
for r in bp.to_dict("records"):
    p = r["pipeline"]
    slots = {d: sp[p]["slots"][d]["slots"] for d in DIMS}
    nok += check(f"parser/{p}", I.parser_file(p, r, pds, slots, sp[p]["timing_cost"]), f"index/parsers/{p}.json")
    gen = I.site_pipeline(r, sp[p]["has_prompt"], slots, sp[p]["timing_cost"])
    rr = same(gen, sp[p], f"site/{p}")
    if rr: fails += 1; print("MISMATCH", rr[:300])
print(f"parsers ok {nok}/{len(bp)}; floats exact={stats['exact']} close(<=1e-12)={stats['close']}")
print("INDEX", "PASS" if not fails else f"FAIL ({fails})")
