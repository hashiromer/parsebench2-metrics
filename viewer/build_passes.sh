#!/usr/bin/env bash
# Full re-run build: build.py --replace for every pipeline of pipelines.json already in the base, --add for the rest
# (providers from pipelines.json), in passes (memory), each pass building on the previous pass's merged tree. Used
# for the 2026-11-24 re-score (61 pipelines, 7 passes). full_rerun.sh calls it.
#
#   viewer/build_passes.sh <work dir> <deliver dir> <results dir> <leaderboard csv> \
#       <results revision> <scorer revision> <groundtruth revision> [<per pass, default 10>]
#
# <work dir>/stage0 must already hold the dataset at the base commit, ground-truth files included (fetch_base.py,
# plus groundtruth/ and bundles/groundtruth/), and <work dir>/gt-out the output of groundtruth.py at the new revision
# (leave it out, or empty, if the ground truth didn't change). <deliver dir> is `gh run download` of the workflow
# (searched recursively for <pipeline>.jsonl.gz), <results dir> holds results/<pipeline>/*.jsonl (fetch_outputs.py).
#
# Writes <work dir>/stage<last>, the final tree (every file; the unchanged ones hard links into stage0; the
# intermediate stages and pass outputs are removed as it goes), <work dir>/pass<k>/build_state.json per pass, and <work dir>/build_state.json for finalize.py. One pipeline
# adds about 300-500 MB in build.py; passes of 10 peaked at 5.7 GB and took about 3.5 min each (2026-11-24).
#
# Trees are chained with hard links (cp -al), so a file is never copied: anything that later writes into a
# stage tree must replace files (write a new file, rename it over the old one), never write through them, or it
# changes stage0 too. finalize.py does this.
set -euo pipefail
[ $# -ge 7 ] || { sed -n 2,20p "$0"; exit 2; }
W=$(realpath "$1"); D=$(realpath "$2"); R=$(realpath "$3"); LB=$(realpath "$4")
RESULTS_REV=$5; SCORER_REV=$6; GT_REV=$7; PER=${8:-10}
V=$(cd "$(dirname "$0")" && pwd)
[ -d "$W/stage0" ] || { echo "no $W/stage0 (the base, with ground-truth files)"; exit 2; }
cd "$V"

rm -rf "$W/stage1"; cp -al "$W/stage0" "$W/stage1"
if [ -d "$W/gt-out" ] && [ -n "$(ls -A "$W/gt-out")" ]; then cp -alf "$W/gt-out/." "$W/stage1/"; echo "ground truth from gt-out"; fi

export PROVIDERS=$(python3 -c 'import json; print(json.dumps({r["pipeline"]: r["provider"] for r in json.load(open("../pipelines.json"))}))')
mapfile -t SPECS < <(uv run -q --with pyarrow python - "$D" "$R" "$W/stage0" <<'PY'
import glob, json, sys
import pyarrow.parquet as pq
D, R, B = sys.argv[1:4]
have = set(pq.read_table(f"{B}/pipelines.parquet", columns=["pipeline"]).column(0).to_pylist())
for r in json.load(open("../pipelines.json")):
    p = r["pipeline"]
    d = glob.glob(f"{D}/**/{p}.jsonl.gz", recursive=True)
    assert len(d) == 1, f"{p}: {len(d)} deliverables under {D}"
    assert len(glob.glob(f"{R}/results/{p}/*.jsonl")) == 13, f"{p}: outputs missing under {R}/results"
    flag = "--replace" if p in have else "--add"
    print(f"{flag} {p}:{d[0]}:{d[0][:-len('.jsonl.gz')]}.next_score.json:{R}/results/{p}:{r['results_revision']}")
PY
)
echo "${#SPECS[@]} pipelines, $PER per pass"
k=1
for ((i = 0; i < ${#SPECS[@]}; i += PER)); do
  args=(); for s in "${SPECS[@]:i:PER}"; do args+=(${s%% *} "${s#* }"); done
  echo "== pass $k: $((${#args[@]} / 2)) pipelines, $(date -u +%T)"
  rm -rf "$W/pass$k"; mkdir -p "$W/pass$k"
  /usr/bin/time -f "pass $k: %e s, max RSS %M KB" uv run -q --with pandas --with pyarrow python build.py \
    --base "$W/stage$k" --out "$W/pass$k/out" --leaderboard "$LB" --results-revision "$RESULTS_REV" \
    --scorer-revision "$SCORER_REV" --groundtruth-revision "$GT_REV" "${args[@]}" 2>&1 | grep -v " rank " \
    || { echo "PASS $k FAILED"; exit 1; }
  test -s "$W/pass$k/build_state.json"
  rm -rf "$W/stage$((k + 1))"; cp -al "$W/stage$k" "$W/stage$((k + 1))"; cp -alf "$W/pass$k/out/." "$W/stage$((k + 1))/"
  rm -rf "$W/stage$k" "$W/pass$k/out"  # stage k+1 holds links to what it needs; this keeps the disk use to ~2 trees
  k=$((k + 1))
done

python3 - "$W" "$((k - 1))" <<'PY'
import json, sys
W, n = sys.argv[1], int(sys.argv[2])
states = [json.load(open(f"{W}/pass{i}/build_state.json")) for i in range(1, n + 1)]
replaced = sorted({p for s in states for p in s["replaced"]})
new = sorted({p for s in states for p in s["new"]})
want = {r["pipeline"] for r in json.load(open("../pipelines.json"))}
assert set(replaced) | set(new) == want and not set(replaced) & set(new), "not every pipeline was rebuilt"
meta = {}
for s in states:
    meta.update(s["meta"])
json.dump({"ranked": states[-1]["ranked"], "new": new, "replaced": replaced, "meta": meta},
          open(f"{W}/build_state.json", "w"), indent=1)
print(f"build_state.json: {len(replaced)} replaced, {len(new)} added")
PY
echo "FINAL TREE: $W/stage$k"
