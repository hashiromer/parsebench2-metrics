"""Write viewer_manifest.json, validation_report.json and README.md for the merged dataset into --out."""
import glob
import hashlib
import json
import os
import re
import sys

import pandas as pd
import pyarrow.parquet as pq

BASE, OUT, STATE, CHECKS, CFG = sys.argv[1:6]
cfg = json.load(open(CFG))
state = json.load(open(STATE))
checks = json.load(open(CHECKS))
NEW = state["new"]


def P(path):
    return f"{OUT}/{path}" if os.path.exists(f"{OUT}/{path}") else f"{BASE}/{path}"


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


# ---- manifest ------------------------------------------------------------------------------------------
m = json.load(open(f"{BASE}/viewer_manifest.json"))
src = m["sources"]
src["results"]["revision"] = cfg["results_revision"]
src["leaderboard"]["revision"] = cfg["leaderboard_revision"]
for p in NEW:
    src["metrics_files"][p] = state["meta"][p]["metrics_file_sha256"]
src["metrics_build"]["note"] = cfg["metrics_build_note"]
for k in list(m["row_counts"]):  # files not in the base download are unchanged: keep their recorded values
    if os.path.exists(P(k)):
        m["row_counts"][k] = pq.ParquetFile(P(k)).metadata.num_rows
for k in list(m["parquet_sha256"]):
    if os.path.exists(P(k)):
        m["parquet_sha256"][k] = sha(P(k))
m["deviations_from_build_spec"] = [cfg["deviation_replace"].get(d[:40], d) for d in m["deviations_from_build_spec"]] \
    + cfg["deviations_add"]
open(f"{OUT}/viewer_manifest.json", "w").write(json.dumps(m, indent=1, ensure_ascii=False) + "\n")

# ---- validation report -----------------------------------------------------------------------------------
v = json.load(open(f"{BASE}/validation_report.json"))
c = v["checks"]
s = pd.read_parquet(P("metrics/scores.parquet"))
pl = pd.read_parquet(P("pipelines.parquet"))
c["1_inputs"].update(metric_files=len(pl), results_files=13 * len(pl))
c["2_row_counts"].update(scores=len(s), pipelines=len(pl))
c["3_keys"].update(min_rows_per_page=checks["2_3_rows_keys"]["min_rows_per_page"],
                   max_rows_per_page=checks["2_3_rows_keys"]["max_rows_per_page"])
c["4_join_exact"]["rows_checked"] += checks["4_join_exact_new"]["rows_checked"]
c["4_join_exact"]["pass"] = c["4_join_exact"]["pass"] and checks["4_join_exact_new"]["pass"]
for mname, n in checks["6_events_complete_new"]["event_rows_new"].items():
    c["6_events_complete"]["event_rows"][mname] = pq.ParquetFile(P(f"metrics/events/{mname}.parquet")).metadata.num_rows
c["6_events_complete"]["metric_values_compared"] += checks["6_events_complete_new"]["metric_values_compared"]
c["6_events_complete"]["pass"] = c["6_events_complete"]["pass"] and checks["6_events_complete_new"]["pass"]
ch = pd.read_parquet(P("metrics/events/chart_next.parquet"), columns=["kind", "severity"])
c["7_severity_coverage"]["null_mapped_severity_by_kind"]["chart_next"] = ch[ch.severity.isna()].kind.value_counts().to_dict()
c["7_severity_coverage"]["pass"] = c["7_severity_coverage"]["pass"] and checks["7_severity_coverage_new"]["pass"]
for p in NEW:
    g = s[s.pipeline == p]
    c["8_timing_cost"]["latency_null_with_output"] += int(g[g.has_output].latency_ms.isna().sum())
    if g.cost_usd.notna().any():
        c["8_timing_cost"]["parsers_with_cost"] += 1
        c["8_timing_cost"]["null_cost_share_by_parser"][p] = round(float(g.cost_usd.isna().mean()), 6)
pds_rows = pq.ParquetFile(P("metrics/page_dimension_scores.parquet")).metadata.num_rows
c["12_site_index"].update(page_dimension_rows=pds_rows,
                          max_points_lost_vs_score_diff=max(c["12_site_index"]["max_points_lost_vs_score_diff"],
                                                            checks["12_site_index"]["max_points_lost_vs_score_diff"]))
c["12_site_index"]["pass"] = c["12_site_index"]["pass"] and checks["12_site_index"]["pass"]
c["11_public_access"]["note"] = ("CORS and range support are repo-wide; this check was run against the commit named here and is "
                                 "re-run against each new commit after upload.")
c["14_leaderboard"] = {**checks["14_leaderboard"], "leaderboard_commit": cfg["leaderboard_revision"]}
c["15_incremental"] = {"pass": checks["existing_unchanged"]["pass"], "added_pipelines": NEW,
                       "control": cfg["control"],
                       "existing_pipelines_unchanged": checks["existing_unchanged"]["checks"],
                       "rank_moves": checks["existing_unchanged"]["rank_moves"]}
v["all_pass_before_upload"] = all(x.get("pass") is not False for x in c.values())
v["all_pass"] = v["all_pass_before_upload"]
open(f"{OUT}/validation_report.json", "w").write(json.dumps(v, indent=1, ensure_ascii=False, default=str) + "\n")

# ---- README --------------------------------------------------------------------------------------------
t = open(f"{BASE}/README.md", encoding="utf-8").read()
n_old, n_new = cfg["old_count"], len(pl)
for a, b in cfg["readme_replace"]:
    assert a in t, a[:80]
    t = t.replace(a, b)
t = t.replace(f"the {n_old} parsers", f"the {n_new} parsers").replace(f"All {n_old} parsers'", f"All {n_new} parsers'")
t = t.replace(f"| `pipelines.parquet` | {n_old} |", f"| `pipelines.parquet` | {n_new} |")
t = t.replace(f"{n_old} × {n_old} head-to-head", f"{n_new} × {n_new} head-to-head")
rc = m["row_counts"]
t = re.sub(r"\| `metrics/scores.parquet` \| [\d,]+ \|", f"| `metrics/scores.parquet` | {rc['metrics/scores.parquet']:,} |", t)
t = re.sub(r"\| `metrics/page_dimension_scores.parquet` \| [\d,]+ \|",
           f"| `metrics/page_dimension_scores.parquet` | {rc['metrics/page_dimension_scores.parquet']:,} |", t)
ev = sum(rc[k] for k in rc if k.startswith("metrics/events/"))
t = re.sub(r"\| `metrics/events/<metric>.parquet` \| [\d.]+M \|", f"| `metrics/events/<metric>.parquet` | {ev / 1e6:.2f}M |", t)
t = re.sub(r"\| `metrics/layout_elements.parquet` \| [\d,]+ \|", f"| `metrics/layout_elements.parquet` | {rc['metrics/layout_elements.parquet']:,} |", t)
t = re.sub(r"\| `metrics/layout_predictions.parquet` \| [\d,]+ \|", f"| `metrics/layout_predictions.parquet` | {rc['metrics/layout_predictions.parquet']:,} |", t)
top = {os.path.basename(f) for root in (BASE, OUT) for f in glob.glob(f"{root}/index/*.json")}
dims = {os.path.basename(f) for root in (BASE, OUT) for f in glob.glob(f"{root}/index/dimensions/*.json")}
n_index = len(top) + len(dims) + len(pl)
t = re.sub(r"\| `index/\*.json` \| \d+ \|", f"| `index/*.json` | {n_index} |", t)
fr = pl.bbox_frame.value_counts()
t = re.sub(r"`fraction` for \d+ parsers", f"`fraction` for {int(fr.get('fraction', 0))} parsers", t)
open(f"{OUT}/README.md", "w", encoding="utf-8").write(t)
print("manifest, validation_report, README written; all_pass_before_upload =", v["all_pass_before_upload"])
