"""Write viewer_manifest.json, validation_report.json and README.md for the merged dataset into --out.

After a full re-run (every pipeline rebuilt: build_passes.sh), the report's counters are set from this build's
checks and data instead of added to, and the scorer and ground-truth revisions come from finalize_cfg.json
("scorer_revision", "groundtruth_revision"). Replacing only some pipelines isn't supported here: the base report
can't be split per pipeline.
"""
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
NEW = state["new"]  # added
REPLACED = state.get("replaced", [])


def P(path):
    return f"{OUT}/{path}" if os.path.exists(f"{OUT}/{path}") else f"{BASE}/{path}"


def write(path, text):
    """Write a new file and rename it over path. Never write in place: in a tree assembled with hard links
    (build_passes.sh) the old file is the base's, and writing through the link would change the base too."""
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


# ---- manifest ------------------------------------------------------------------------------------------
m = json.load(open(f"{BASE}/viewer_manifest.json"))
src = m["sources"]
src["results"]["revision"] = cfg["results_revision"]
src["leaderboard"]["revision"] = cfg["leaderboard_revision"]
for p in NEW + REPLACED:
    src["metrics_files"][p] = state["meta"][p]["metrics_file_sha256"]
if cfg.get("scorer_revision"):
    src["scorer"]["revision"] = cfg["scorer_revision"]
if cfg.get("groundtruth_revision"):
    src["groundtruth"]["revision"] = cfg["groundtruth_revision"]
src["metrics_build"]["note"] = cfg["metrics_build_note"]
for k in list(m["row_counts"]):  # files not in the base download are unchanged: keep their recorded values
    if os.path.exists(P(k)):
        m["row_counts"][k] = pq.ParquetFile(P(k)).metadata.num_rows
for k in list(m["parquet_sha256"]):
    if os.path.exists(P(k)):
        m["parquet_sha256"][k] = sha(P(k))
m["deviations_from_build_spec"] = [cfg["deviation_replace"].get(d[:40], d) for d in m["deviations_from_build_spec"]] \
    + cfg["deviations_add"]
write(f"{OUT}/viewer_manifest.json", json.dumps(m, indent=1, ensure_ascii=False) + "\n")

# ---- validation report -----------------------------------------------------------------------------------
v = json.load(open(f"{BASE}/validation_report.json"))
c = v["checks"]
s = pd.read_parquet(P("metrics/scores.parquet"))
pl = pd.read_parquet(P("pipelines.parquet"))
FULL = bool(REPLACED)
if FULL:
    assert set(NEW) | set(REPLACED) == set(pl.pipeline), "a full re-run rebuilds every pipeline; replacing only some isn't supported"
c["1_inputs"].update(metric_files=len(pl), results_files=13 * len(pl))
c["2_row_counts"].update(scores=len(s), pipelines=len(pl))
c["3_keys"].update(min_rows_per_page=checks["2_3_rows_keys"]["min_rows_per_page"],
                   max_rows_per_page=checks["2_3_rows_keys"]["max_rows_per_page"])
c["4_join_exact"]["rows_checked"] = (0 if FULL else c["4_join_exact"]["rows_checked"]) + checks["4_join_exact_new"]["rows_checked"]
c["4_join_exact"]["pass"] = c["4_join_exact"]["pass"] and checks["4_join_exact_new"]["pass"]
for mname, n in checks["6_events_complete_new"]["event_rows_new"].items():
    c["6_events_complete"]["event_rows"][mname] = pq.ParquetFile(P(f"metrics/events/{mname}.parquet")).metadata.num_rows
c["6_events_complete"]["metric_values_compared"] = (0 if FULL else c["6_events_complete"]["metric_values_compared"]) \
    + checks["6_events_complete_new"]["metric_values_compared"]
c["6_events_complete"]["pass"] = c["6_events_complete"]["pass"] and checks["6_events_complete_new"]["pass"]
ch = pd.read_parquet(P("metrics/events/chart_next.parquet"), columns=["kind", "severity"])
c["7_severity_coverage"]["null_mapped_severity_by_kind"]["chart_next"] = ch[ch.severity.isna()].kind.value_counts().to_dict()
c["7_severity_coverage"]["pass"] = c["7_severity_coverage"]["pass"] and checks["7_severity_coverage_new"]["pass"]
if FULL:  # every pipeline rebuilt, outputs may have changed too: count over all of them (reproduces 2026-11-24's values)
    c["8_timing_cost"].update(latency_null_with_output=0, parsers_with_cost=0, null_cost_share_by_parser={})
for p in (sorted(pl.pipeline) if FULL else NEW):
    g = s[s.pipeline == p]
    c["8_timing_cost"]["latency_null_with_output"] += int(g[g.has_output].latency_ms.isna().sum())
    if g.cost_usd.notna().any():
        c["8_timing_cost"]["parsers_with_cost"] += 1
        c["8_timing_cost"]["null_cost_share_by_parser"][p] = round(float(g.cost_usd.isna().mean()), 6)
pds_rows = pq.ParquetFile(P("metrics/page_dimension_scores.parquet")).metadata.num_rows
c["12_site_index"].update(page_dimension_rows=pds_rows,
                          max_points_lost_vs_score_diff=checks["12_site_index"]["max_points_lost_vs_score_diff"] if FULL else
                          max(c["12_site_index"]["max_points_lost_vs_score_diff"], checks["12_site_index"]["max_points_lost_vs_score_diff"]))
c["12_site_index"]["pass"] = c["12_site_index"]["pass"] and checks["12_site_index"]["pass"]
c["11_public_access"]["note"] = ("CORS and range support are repo-wide; this check was run against the commit named here and is "
                                 "re-run against each new commit after upload.")
c["14_leaderboard"] = {**checks["14_leaderboard"], "leaderboard_commit": cfg["leaderboard_revision"]}
if FULL:  # every pipeline re-scored: nothing is "existing", the control and replace proof are the checks
    c.pop("15_incremental", None)
    c["15_rescore"] = {"pass": checks["existing_unchanged"]["pass"], "replaced_pipelines": REPLACED,
                       "added_pipelines": NEW, "scorer": cfg["scorer_revision"],
                       "groundtruth": cfg["groundtruth_revision"], "control": cfg["control"],
                       "replace_proof": cfg["replace_proof"]}
else:
    c["15_incremental"] = {"pass": checks["existing_unchanged"]["pass"], "added_pipelines": NEW,
                           "control": cfg["control"],
                           "existing_pipelines_unchanged": checks["existing_unchanged"]["checks"],
                           "rank_moves": checks["existing_unchanged"]["rank_moves"]}
v["all_pass_before_upload"] = all(x.get("pass") is not False for x in c.values())
v["all_pass"] = v["all_pass_before_upload"]
write(f"{OUT}/validation_report.json", json.dumps(v, indent=1, ensure_ascii=False, default=str) + "\n")

# ---- README --------------------------------------------------------------------------------------------
t = open(f"{BASE}/README.md", encoding="utf-8").read()
n_old, n_new = cfg["old_count"], len(pl)
# The dataset README says for how many parsers the unrounded mean differs from the published Overall, and by how
# much. Write "{overall_off_count}" and "{overall_off_range}" in readme_replace instead of counting by hand.
off = sorted(abs(x - float(y)) for _, x, y in checks["14_leaderboard"]["info_exact_overall_beyond_rounding"])
fill = {"{overall_off_count}": str(len(off)),
        "{overall_off_range}": f"{off[0]:.3f}–{off[-1]:.3f}" if off else "0"}
for a, b in cfg["readme_replace"]:
    assert a in t, a[:80]
    for k, val in fill.items():
        b = b.replace(k, val)
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
write(f"{OUT}/README.md", t)
print("manifest, validation_report, README written; all_pass_before_upload =", v["all_pass_before_upload"])
