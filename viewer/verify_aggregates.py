"""Rebuild the base dataset's aggregates from its own per-page rows and compare with what is published."""
import glob
import json
import sys

import pandas as pd

import aggregates as A

BASE, LB = sys.argv[1], sys.argv[2]
fails = 0


def cmp_df(name, gen, base):
    global fails
    gen = gen.reset_index(drop=True); base = base.reset_index(drop=True)
    if len(gen) != len(base) or list(gen.columns) != list(base.columns):
        print(f"[{name}] shape gen={gen.shape} base={base.shape} cols_diff={set(gen.columns) ^ set(base.columns)}")
        fails += 1; return
    bad = []
    for c in base.columns:
        a, b = gen[c], base[c]
        if pd.api.types.is_float_dtype(b):
            eq = (a.isna() & b.isna()) | ((a.astype(float) - b.astype(float)).abs() <= 1e-12)
        else:
            eq = (a.isna() & b.isna()) | (a.astype(object) == b.astype(object))
        if not eq.all():
            i = (~eq).idxmax(); bad.append((c, int((~eq).sum()), repr(a[i])[:70], repr(b[i])[:70]))
    print(f"[{name}] rows={len(base)} mismatched columns={len(bad)}")
    for x in bad[:12]: print("   ", x)
    fails += bool(bad)


scores = pd.read_parquet(f"{BASE}/metrics/scores.parquet")
slots = pd.read_parquet(f"{BASE}/slots.parquet")
bp = pd.read_parquet(f"{BASE}/pipelines.parquet")
site = json.load(open(f"{BASE}/index/site.json"))

# bbox_frame from the layout bundles
boxes = {p: [] for p in bp.pipeline}
for f in glob.glob(f"{BASE}/bundles/layout_boxes/layout/*.json"):
    for p, v in json.load(open(f))["parsers"].items():
        boxes[p].append(v)
frames = {p: A.bbox_frame(v) for p, v in boxes.items()}
badf = {p: (frames[p], f) for p, f in zip(bp.pipeline, bp.bbox_frame) if frames[p] != (None if pd.isna(f) else f)}
print("[bbox_frame] mismatches:", badf or "none"); fails += bool(badf)

meta = {r.pipeline: {"provider": r.provider, "results_revision": r.results_revision,
                     "metrics_file_sha256": r.metrics_file_sha256, "bbox_frame": r.bbox_frame} for r in bp.itertuples()}
gen, st, tcs = A.pipelines_table(list(bp.pipeline), A.read_leaderboard(LB), scores, slots, meta)
cmp_df("pipelines", gen[bp.columns.tolist()], bp)

sp = {p["pipeline"]: p for p in site["pipelines"]}
badt = [p for p in bp.pipeline if json.dumps(tcs[p], sort_keys=True) != json.dumps(sp[p]["timing_cost"], sort_keys=True)]
print("[timing_cost] mismatches:", len(badt), [(p, tcs[p], sp[p]["timing_cost"]) for p in badt[:2]]); fails += bool(badt)
def _slot_eq(a, b):
    return len(a) == len(b) and all({k: x[k] for k in x if k != "score"} == {k: y[k] for k in y if k != "score"}
                                    and ((x["score"] is None and y["score"] is None) or abs(x["score"] - y["score"]) <= 1e-12)
                                    for x, y in zip(a, b))
bads = [(p, d) for p in bp.pipeline for d in A.DIMS if not _slot_eq(st[(p, d)], sp[p]["slots"][d]["slots"])]
exact = sum(st[(p, d)] == sp[p]["slots"][d]["slots"] for p in bp.pipeline for d in A.DIMS)
print(f"[slots] bit-exact {exact}/{len(bp) * 6} (within 1e-12 required for all)")
print("[slots] mismatches:", len(bads), bads[:3]); fails += bool(bads)
if bads:
    p, d = bads[0]; print("   gen", st[(p, d)]); print("   base", sp[p]["slots"][d]["slots"])

pds = pd.read_parquet(f"{BASE}/metrics/page_dimension_scores.parquet")
cmp_df("page_dimension_scores", A.page_dimension_scores(scores, slots), pds)
print("AGGREGATES", "PASS" if not fails else f"FAIL ({fails})")
