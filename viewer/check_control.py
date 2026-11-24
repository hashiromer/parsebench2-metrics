"""Regenerate one pipeline already in the base dataset and compare every per-pipeline row with the base."""
import json
import sys

import pandas as pd

import rows as R

BASE = sys.argv[1]          # base dataset dir
P = sys.argv[2]             # pipeline
DELIV = sys.argv[3]         # <pipeline>.jsonl.gz
RAWDIR = sys.argv[4]        # results/<pipeline>/
REV = sys.argv[5]           # its results revision

man = json.load(open(f"{BASE}/viewer_manifest.json"))
lines = R.load_deliverable(DELIV)
raw = R.load_raw(RAWDIR)
fails = 0


def cmp(name, gen: pd.DataFrame, base: pd.DataFrame, keys):
    global fails
    gen = gen.sort_values(keys).reset_index(drop=True)
    base = base.sort_values(keys).reset_index(drop=True)
    if len(gen) != len(base):
        print(f"[{name}] row count gen={len(gen)} base={len(base)}"); fails += 1; return
    bad = []
    for c in base.columns:
        a, b = gen[c], base[c]
        try:
            eq = (a.isna() & b.isna()) | (a.astype(object) == b.astype(object))
        except Exception:
            eq = pd.Series([str(x) == str(y) for x, y in zip(a, b)])
        if not eq.all():
            i = (~eq).idxmax()
            bad.append((c, int((~eq).sum()), repr(a[i])[:80], repr(b[i])[:80]))
    print(f"[{name}] rows={len(base)} mismatched columns={len(bad)}")
    for x in bad[:20]:
        print("   ", x)
    fails += bool(bad)


s = pd.read_parquet(f"{BASE}/metrics/scores.parquet")
bs = s[s.pipeline == P]
g = R.score_rows(P, lines, raw, list(s.columns))
cmp("scores", g, bs, ["subset", "test_id"])

evs = R.event_rows(P, lines, man["severity_maps"])
for m, rs in evs.items():
    be = pd.read_parquet(f"{BASE}/metrics/events/{m}.parquet")
    be = be[be.pipeline == P]
    cmp(f"events/{m}", pd.DataFrame(rs, columns=be.columns), be, ["subset", "test_id", "event_index"])

els, preds = R.layout_rows(P, lines)
be = pd.read_parquet(f"{BASE}/metrics/layout_elements.parquet"); be = be[be.pipeline == P]
cmp("layout_elements", pd.DataFrame(els, columns=be.columns), be, ["test_id", "element_id", "page"])
bp = pd.read_parquet(f"{BASE}/metrics/layout_predictions.parquet"); bp = bp[bp.pipeline == P]
cmp("layout_predictions", pd.DataFrame(preds, columns=bp.columns), bp, ["test_id", "page", "pred_index"])

ents = R.bundle_entries(P, lines, raw, REV)
nb = [0, 0, 0]; bad = [0, 0, 0]
for (subset, pk), (me, oe, le) in ents.items():
    b = json.load(open(f"{BASE}/bundles/metrics/{subset}/{pk}.json"))["parsers"][P]
    nb[0] += 1; bad[0] += b != me
    if b != me and bad[0] < 3:
        print("   metrics diff", subset, pk, {k: (str(me[k])[:60], str(b[k])[:60]) for k in b if b[k] != me.get(k)})
    b = json.load(open(f"{BASE}/bundles/outputs/{subset}/{pk}.json"))["parsers"][P]
    nb[1] += 1; bad[1] += b != oe
    if b != oe and bad[1] < 3:
        print("   outputs diff", subset, pk, {k: (str(oe[k])[:60], str(b[k])[:60]) for k in b if b[k] != oe.get(k)})
    if subset == "layout":
        b = json.load(open(f"{BASE}/bundles/layout_boxes/layout/{pk}.json"))["parsers"][P]
        nb[2] += 1; bad[2] += b != le
print(f"[bundles] metrics {nb[0]-bad[0]}/{nb[0]} outputs {nb[1]-bad[1]}/{nb[1]} layout_boxes {nb[2]-bad[2]}/{nb[2]}")
fails += sum(bad)
print("CONTROL", "PASS" if not fails else f"FAIL ({fails})")
sys.exit(1 if fails else 0)
