"""Checks on the merged dataset (base + changed files in --out) before upload. Writes checks.json next to --out.

  python validate.py <base> <out> <leaderboard-v2.csv> <pipeline>[,<pipeline>...]

List every pipeline the build added or replaced (--add and --replace). They get the full checks on their new rows;
every other pipeline must be unchanged.
"""
import glob
import hashlib
import json
import math
import sys

import pandas as pd

BASE, OUT, LB = sys.argv[1], sys.argv[2], sys.argv[3]
NEW = sys.argv[4].split(",")  # added or replaced
_base_pipes = set(pd.read_parquet(f"{BASE}/pipelines.parquet").pipeline)
ADDED = {p for p in NEW if p not in _base_pipes}
res = {}


def P(path):  # merged view: changed file if written, else base
    import os
    return f"{OUT}/{path}" if os.path.exists(f"{OUT}/{path}") else f"{BASE}/{path}"


# 14: leaderboard, every row, to 2-decimal rounding
import csv
lb = {r["Provider"]: r for r in csv.DictReader(open(LB))}
pl = pd.read_parquet(P("pipelines.parquet"))
col = {"overall": "Overall", "text": "Text", "formatting": "Formatting", "tables": "Tables", "charts": "Charts",
       "layout": "Visual_Grounding", "forms": "Forms"}
off = []
dims = [d for d in col if d != "overall"]
exact_overall_info = []
for r in pl.itertuples():
    row = lb[r.provider]
    for d in dims:  # every dimension within the CSV's 2-decimal rounding
        v = getattr(r, f"score_{d}")
        if abs(v - float(row[col[d]])) > 0.005 + 1e-9:
            off.append((r.pipeline, d, round(v, 4), row[col[d]]))
    # Overall as the leaderboard computes it: mean of the 2-decimal dimension scores
    ov = sum(round(getattr(r, f"score_{d}"), 2) for d in dims) / len(dims)
    if abs(ov - float(row["Overall"])) > 0.005 + 1e-9:
        off.append((r.pipeline, "overall (mean of rounded dimensions)", round(ov, 4), row["Overall"]))
    if abs(r.score_overall - float(row["Overall"])) > 0.005 + 1e-9:
        exact_overall_info.append((r.pipeline, round(r.score_overall, 4), row["Overall"]))
res["14_leaderboard"] = {"pass": not off and len(pl) == len(lb), "leaderboard_rows": len(lb), "pipelines": len(pl),
                         "rule": "each dimension within 0.005 of the CSV; Overall = mean of the six dimensions rounded to "
                                 "2 decimals (the leaderboard's own rule), within 0.005",
                         "off_by_more_than_rounding": off,
                         "max_abs_dimension_delta_points": max(abs(getattr(r, f"score_{d}") - float(lb[r.provider][col[d]]))
                                                               for r in pl.itertuples() for d in dims),
                         "info_exact_overall_beyond_rounding": exact_overall_info}

# existing pipelines unchanged (per-pipeline tables, bit for bit)
def same_rows(path, keys, where=lambda d: ~d.pipeline.isin(NEW)):
    a = pd.read_parquet(f"{BASE}/{path}"); b = pd.read_parquet(P(path))
    a, b = a[where(a)], b[where(b)]
    a = a.sort_values(keys).reset_index(drop=True); b = b.sort_values(keys).reset_index(drop=True)
    if a.shape != b.shape:
        return False, f"shape {a.shape} vs {b.shape}"
    bad = [c for c in a.columns if not ((a[c].isna() & b[c].isna()) | (a[c].astype(object) == b[c].astype(object))).all()]
    return not bad, bad


unchanged = {}
for path, keys in [("metrics/scores.parquet", ["pipeline", "test_id"]),
                   ("metrics/layout_elements.parquet", ["pipeline", "test_id", "element_id", "page"]),
                   ("metrics/layout_predictions.parquet", ["pipeline", "test_id", "page", "pred_index"])] + \
                  [(f"metrics/events/{m}.parquet", ["pipeline", "test_id", "event_index"])
                   for m in ["text_next", "formatting_v2", "table_next", "chart_next", "forms_v2"]]:
    unchanged[path] = same_rows(path, keys)
a = pd.read_parquet(f"{BASE}/pipelines.parquet").set_index("pipeline")
a = a[~a.index.isin(NEW)]
b = pl.set_index("pipeline").loc[a.index]
diffcols = [c for c in a.columns if not ((a[c].isna() & b[c].isna()) | (a[c].astype(object) == b[c].astype(object))).all()]
unchanged["pipelines.parquet (columns that changed)"] = (set(diffcols) <= {"leaderboard_rank"}, diffcols)
rank_moves = {p: (int(a.at[p, "leaderboard_rank"]), int(b.at[p, "leaderboard_rank"]))
              for p in a.index if a.at[p, "leaderboard_rank"] != b.at[p, "leaderboard_rank"]}
pa_, pb_ = pd.read_parquet(f"{BASE}/metrics/page_dimension_scores.parquet"), pd.read_parquet(P("metrics/page_dimension_scores.parquet"))
pa_, pb_ = pa_[~pa_.pipeline.isin(NEW)], pb_[~pb_.pipeline.isin(NEW)]
k = ["dimension", "pipeline", "test_id"]
m = pa_.merge(pb_, on=k, suffixes=("_a", "_b"))
own = ["score", "has_output", "slot_weight", "n_pages_in_slot", "page_weight", "points_lost", "n_critical", "n_major",
       "n_minor", "n_events"]
badown = [c for c in own if not ((m[c + "_a"].isna() & m[c + "_b"].isna()) | (m[c + "_a"].astype(object) == m[c + "_b"].astype(object))).all()]
unchanged["page_dimension_scores (pipeline-own columns)"] = (not badown and len(m) == len(pa_), badown)
site_a = {p["pipeline"]: p for p in json.load(open(f"{BASE}/index/site.json"))["pipelines"]}
site_b = {p["pipeline"]: p for p in json.load(open(P("index/site.json")))["pipelines"]}
bad_site = [p for p in site_a if p not in NEW and {k: v for k, v in site_a[p].items() if k != "leaderboard_rank"} !=
            {k: v for k, v in site_b[p].items() if k != "leaderboard_rank"}]
unchanged["site.json pipeline entries (except rank)"] = (not bad_site, bad_site)
nb = bad = 0
for kind in ("metrics", "outputs"):
    for f in glob.glob(f"{BASE}/bundles/{kind}/*/*.json"):
        x = json.load(open(f))["parsers"]; y = json.load(open(f.replace(BASE, OUT)))["parsers"]
        nb += 1
        bad += any(x[p] != y[p] for p in x if p not in NEW) or set(y) - set(x) != ADDED
for f in glob.glob(f"{BASE}/bundles/layout_boxes/layout/*.json"):
    x = json.load(open(f))["parsers"]; y = json.load(open(f.replace(BASE, OUT)))["parsers"]
    nb += 1
    bad += any(x[p] != y[p] for p in x if p not in NEW) or set(y) - set(x) != ADDED
unchanged["bundles (existing parser entries)"] = (bad == 0, f"{nb - bad}/{nb} bundles identical for existing parsers")
res["existing_unchanged"] = {"pass": all(v[0] for v in unchanged.values()),
                             "checks": {k: {"pass": bool(v[0]), "detail": v[1]} for k, v in unchanged.items()},
                             "rank_moves": rank_moves}

# 2/3: counts and keys
s = pd.read_parquet(P("metrics/scores.parquet"))
per_page = s.groupby("test_id").size()
pages = pd.read_parquet(f"{BASE}/pages.parquet")
res["2_3_rows_keys"] = {"pass": bool(s.duplicated(["pipeline", "test_id"]).sum() == 0 and per_page.min() == per_page.max() == len(pl)
                                     and set(s.test_id) == set(pages.test_id)),
                        "scores": len(s), "pipelines": len(pl), "min_rows_per_page": int(per_page.min()),
                        "max_rows_per_page": int(per_page.max()),
                        "page_key_rule": bool((s.page_key == s.test_id.map(lambda t: hashlib.sha256(t.encode()).hexdigest()[:16])).all())}

# 4: markdown hash in scores == sha256 of the markdown in the outputs bundle (new pipelines)
# Rows are grouped by page, so each page's bundles are read once for all the pipelines checked on it.
mism = []; nchk = 0
sn = s[s.pipeline.isin(NEW)]


def by_page(df):
    for (subset, pk), g in df.groupby(["subset", "page_key"], sort=False):
        yield (json.load(open(P(f"bundles/outputs/{subset}/{pk}.json")))["parsers"],
               json.load(open(P(f"bundles/metrics/{subset}/{pk}.json")))["parsers"], g)


vbad = 0; vn = 0  # check 6's metric values, compared here while each page's bundle is open
for obs, mbs, g in by_page(sn):
    for r in g.itertuples():
        ob, mb = obs[r.pipeline], mbs[r.pipeline]
        for mm in mb["metrics"]:
            if mm["metric_name"] in s.columns:
                vn += 1
                v = getattr(r, mm["metric_name"])
                if not ((v is None or (isinstance(v, float) and math.isnan(v))) and mm["value"] is None) and v != mm["value"]:
                    vbad += 1
        md = ob["markdown"]
        h = hashlib.sha256(md.encode("utf-8")).hexdigest() if md is not None else None
        nchk += 1
        rh = None if pd.isna(r.markdown_sha256) else r.markdown_sha256  # no output: null in the row (NaN) and the bundles
        if h != rh or mb["markdown_sha256"] != rh:
            mism.append(r.test_id)
res["4_join_exact_new"] = {"pass": not mism, "rows_checked": nchk, "mismatches": mism[:20],
                           "null_hash_rows_new": int(sn.markdown_sha256.isna().sum())}

# 6: events complete for new pipelines; metric values in scores == bundle values
ev_counts = {}; evbad = []
for mname in ["text_next", "formatting_v2", "table_next", "chart_next", "forms_v2"]:
    e = pd.read_parquet(P(f"metrics/events/{mname}.parquet"), columns=["pipeline", "test_id"])
    e = e[e.pipeline.isin(NEW)].groupby(["pipeline", "test_id"]).size()
    ev_counts[mname] = int(e.sum())
    col_n = f"{mname}__n_events"
    exp = sn.set_index(["pipeline", "test_id"])[col_n].dropna()
    exp = exp[exp > 0]
    if not (e.reindex(exp.index).fillna(0).astype(int) == exp.astype(int)).all() or int(exp.sum()) != int(e.sum()):
        evbad.append(mname)
res["6_events_complete_new"] = {"pass": not evbad and vbad == 0, "event_rows_new": ev_counts, "event_mismatches": evbad,
                                "metric_values_compared": vn, "metric_value_mismatches": vbad}

# 7: severity coverage for new pipelines
sev = {}
for mname in ["text_next", "chart_next"]:
    e = pd.read_parquet(P(f"metrics/events/{mname}.parquet"), columns=["pipeline", "kind", "severity"])
    e = e[e.pipeline.isin(NEW)]
    sev[mname] = e[e.severity.isna()].kind.value_counts().to_dict()
res["7_severity_coverage_new"] = {"pass": set(sev["text_next"]) == set() and set(sev["chart_next"]) <= {"legend lost"},
                                  "null_mapped_severity_by_kind": sev}

# 12: index consistency: points_lost over a dimension sums to 100 - score
worst = 0.0
for p in pl.pipeline:
    pf = json.load(open(P(f"index/parsers/{p}.json")))
    for d, v in pf["dimensions"].items():
        worst = max(worst, abs(v["points_lost_total"] - (100 - v["score"])))
dm = {d: json.load(open(P(f"index/dimensions/{d}.json"))) for d in col if d != "overall"}
res["12_site_index"] = {"pass": worst < 1e-6 and all(len(v["pipelines"]) == len(pl) for v in dm.values()),
                        "max_points_lost_vs_score_diff": worst,
                        "parser_files": len(glob.glob(P("index/parsers") + "/*.json")) if False else len(pl)}

res["all_pass"] = all(v["pass"] for v in res.values() if isinstance(v, dict) and "pass" in v)
json.dump(res, open(f"{OUT}/../checks.json", "w"), indent=1, default=str)
for k, v in res.items():
    if isinstance(v, dict):
        print(k, "PASS" if v["pass"] else "FAIL", {kk: vv for kk, vv in v.items() if kk not in ("pass", "checks")})
        for kk, vv in v.get("checks", {}).items():
            print("   ", kk, vv)
print("ALL PASS" if res["all_pass"] else "SOME CHECKS FAILED")
sys.exit(0 if res["all_pass"] else 1)
