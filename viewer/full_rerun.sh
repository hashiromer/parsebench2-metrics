#!/usr/bin/env bash
# The FULL_RERUN path, from scored deliverables to a validated dataset tree, in one command:
#
#   viewer/full_rerun.sh <work dir> <changes.json> <Build metrics run id>
#
# <changes.json> is detect_changes.py's report (verdict FULL_RERUN, pipelines.json written with --write-pipelines);
# the run id is the "Build metrics" workflow run that scored every pipeline in pipelines.json. Steps:
#
#   1. the dataset at the report's commit, ground-truth files included          -> <work>/stage0   (fetch_base.py)
#   2. leaderboard-v2.csv at the report's ParseBench commit, rows of held-back
#      pipelines dropped (the dataset ranks only the pipelines it has)          -> <work>/leaderboard-v2.csv
#   3. the run's deliverables, checked against that leaderboard (stops if off)  -> <work>/deliver  (compare_scores.py)
#   4. every pipeline's published outputs at its pin                            -> <work>/results  (fetch_outputs.py)
#   5. ground truth at the new revision, and its tables and bundles             -> <work>/gt, gt-out (groundtruth.py)
#   6. the build, in passes                                                     -> <work>/stage<n> (build_passes.sh)
#   7. validate.py on the final tree, every pipeline listed                     -> <work>/checks.json
#
# Then finalize.py, changeset.py --local, upload and checks by hand (README "Full re-run"). Each step reuses what is
# already on disk, so a failed run can be started again. Optional environment:
#   SEEDS="<dir> ..."          dataset trees to hard-link base files from (previous work dirs' final stage, stage0)
#   RESULTS_SEEDS="<dir> ..."  dirs holding results/<pipeline>/ from earlier runs
#   GT_DIR=<dir>               ParseBench-2 ground truth already downloaded at the new revision (it must hold
#                              .done-<revision>, as score_local.sh and this script write)
#   DELIVER=<dir>              deliverables already downloaded (skips gh run download)
#   FORCE=1                    run although the verdict isn't FULL_RERUN (to test the path on unchanged data)
set -euo pipefail
[ $# -eq 3 ] || { sed -n 2,26p "$0"; exit 2; }
W=$(realpath -m "$1"); REPORT=$(realpath "$2"); RUN=$3
V=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$W"; cd "$V"
PY="uv run -q --with pandas --with pyarrow --with huggingface_hub python"

read -r VERDICT DS PB SCORER GT RPIN < <(python3 -c '
import json, sys; r = json.load(open(sys.argv[1]))
print(r["verdict"], r["dataset_commit"], r["parsebench_commit"], r["scorer"]["upstream"], r["ground_truth"]["upstream"], r["results_pin"])' "$REPORT")
echo "verdict $VERDICT; dataset ${DS:0:7}, ParseBench ${PB:0:7}, scorer ${SCORER:0:7}, ground truth ${GT:0:7}, results ${RPIN:0:7}"
[ "$VERDICT" = FULL_RERUN ] || [ "${FORCE:-}" = 1 ] || { echo "verdict is $VERDICT, not FULL_RERUN: stop"; exit 1; }
python3 - "$REPORT" <<'EOF'  # pipelines.json must be the one the report proposes
import json, sys
r = json.load(open(sys.argv[1])); cur = json.load(open("../pipelines.json"))
key = lambda rows: sorted(json.dumps(x, sort_keys=True) for x in rows)
assert key(cur) == key(r["pipelines"]), "pipelines.json differs from the report's: run detect_changes.py --write-pipelines"
EOF

echo "== 1. base"
# shellcheck disable=SC2086
$PY fetch_base.py "$DS" "$W/stage0" ${SEEDS:-} --gt

echo "== 2. leaderboard"
curl -fsSL "https://raw.githubusercontent.com/run-llama/ParseBench/$PB/leaderboard-v2.csv" -o "$W/leaderboard-v2.upstream.csv"
python3 - "$W" <<'EOF'
import csv, json, sys
W = sys.argv[1]
want = {r["provider"] for r in json.load(open("../pipelines.json"))}
rows = list(csv.DictReader(open(f"{W}/leaderboard-v2.upstream.csv", newline="")))
keep = [r for r in rows if r["Provider"] in want]
assert {r["Provider"] for r in keep} == want, f"no leaderboard row for {sorted(want - {r['Provider'] for r in keep})}"
with open(f"{W}/leaderboard-v2.csv", "w", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n"); w.writeheader(); w.writerows(keep)
print(f"{len(keep)} rows kept; dropped (held back): {sorted(r['Provider'] for r in rows if r not in keep) or '-'}")
EOF

echo "== 3. deliverables"
D=${DELIVER:-$W/deliver}
if [ -z "${DELIVER:-}" ] && [ ! -d "$D" ]; then
  gh run download "$RUN" -R hashiromer/parsebench2-metrics -D "$D.tmp" && mv "$D.tmp" "$D"
fi
python3 compare_scores.py "$D" "$W/leaderboard-v2.csv" ../pipelines.json | tail -3

echo "== 4. outputs"
args=(); for s in ${RESULTS_SEEDS:-}; do args+=(--seed "$s"); done
$PY fetch_outputs.py "$W/results" "${args[@]}"

echo "== 5. ground truth"
G=${GT_DIR:-$W/gt}
if [ -n "${GT_DIR:-}" ] && [ ! -f "$G/.done-$GT" ]; then echo "GT_DIR $G is not ground truth at ${GT:0:7} (no .done-$GT): leave GT_DIR unset to download it"; exit 1; fi
if [ -z "${GT_DIR:-}" ] && [ ! -f "$G/.done-$GT" ]; then
  $PY - "$G" "$GT" <<'EOF'
import sys, time
from huggingface_hub import snapshot_download
for i in range(6):
    try:
        snapshot_download("llamaindex/ParseBench-2", repo_type="dataset", revision=sys.argv[2], local_dir=sys.argv[1],
                          max_workers=4)  # more workers trip HF rate limits (429)
        break
    except Exception as e:
        if i == 5: raise
        print("retry", i, repr(e)[:200], flush=True); time.sleep(15 * 2 ** i)
EOF
  rm -rf "$G/.cache"; touch "$G/.done-$GT"
fi
rm -rf "$W/gt-out"
$PY groundtruth.py "$G" "$GT" "$W/stage0" "$W/gt-out" | tail -2

echo "== 6. build"
./build_passes.sh "$W" "$D" "$W/results" "$W/leaderboard-v2.csv" "$RPIN" "$SCORER" "$GT" | tee "$W/passes.log"
FINAL=$(tail -1 "$W/passes.log" | sed 's/^FINAL TREE: //')

echo "== 7. validate"
ALL=$(python3 -c 'import json; print(",".join(r["pipeline"] for r in json.load(open("../pipelines.json"))))')
$PY validate.py "$W/stage0" "$FINAL" "$W/leaderboard-v2.csv" "$ALL" 2>&1 | grep -v RuntimeWarning | tail -25
echo "VALIDATED TREE: $FINAL (checks: $W/checks.json). Next: finalize.py, changeset.py --local $W/stage0 (README)."
