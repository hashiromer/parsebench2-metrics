#!/usr/bin/env bash
# Score pipelines on this machine, the same way .github/workflows/metrics.yml does (no GitHub Actions).
#
#   viewer/score_local.sh <work dir> <scorer commit> <data revision> <pipeline> [<pipeline> ...]
#
# Each pipeline must be listed in pipelines.json (provider + results_revision). Writes
# <work dir>/deliver/<pipeline>.{jsonl.gz,next_score.json,manifest.json}. The scorer checkout and the
# ground truth are reused across runs; keep <work dir> on the main disk (/tmp is a small in-memory drive).
set -euo pipefail
WORK=$(realpath -m "$1"); SCORER_COMMIT=$2; DATA_REV=$3; shift 3
REPO=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$WORK"

if [ ! -d "$WORK/scorer/.git" ] || [ "$(git -C "$WORK/scorer" rev-parse HEAD)" != "$SCORER_COMMIT" ] \
   || ! git -C "$WORK/scorer" diff --quiet; then
  rm -rf "$WORK/scorer"
  git clone -q https://github.com/run-llama/ParseBench "$WORK/scorer"
  git -C "$WORK/scorer" checkout -q "$SCORER_COMMIT"
fi
# Patches, as in the workflow: full_events.patch (uncapped events) and register_pipelines.patch.
if git -C "$WORK/scorer" diff --quiet; then
  for p in full_events.patch register_pipelines.patch; do git -C "$WORK/scorer" apply "$REPO/$p"; done
fi
uv sync -q --project "$WORK/scorer" --frozen --extra runners --extra fast

if [ ! -f "$WORK/gt/.done-$DATA_REV" ]; then
  uv run -q --with "huggingface_hub>=0.34" python - "$WORK/gt" "$DATA_REV" <<'EOF'
import sys, time
from huggingface_hub import snapshot_download
for i in range(6):
    try:
        snapshot_download("llamaindex/ParseBench-2", repo_type="dataset", revision=sys.argv[2],
                          local_dir=sys.argv[1], max_workers=4)  # more workers trip HF rate limits (429)
        break
    except Exception as e:
        if i == 5: raise
        print("retry", i, repr(e)[:200], flush=True); time.sleep(15 * 2 ** i)
EOF
  rm -rf "$WORK/gt/.cache"; touch "$WORK/gt/.done-$DATA_REV"
fi

PATCH=$(sha256sum "$REPO/full_events.patch" | cut -d' ' -f1)
REG=$(sha256sum "$REPO/register_pipelines.patch" | cut -d' ' -f1)
for P in "$@"; do
  mapfile -t F < <(python3 -c '
import json, sys
r = {x["pipeline"]: x for x in json.load(open(sys.argv[1]))}[sys.argv[2]]
print(r["provider"]); print(r["results_revision"])' "$REPO/pipelines.json" "$P")
  PROVIDER=${F[0]}; REV=${F[1]}
  echo "== scoring $P ($PROVIDER @ ${REV:0:7})"
  (cd "$REPO" && uv run --project "$WORK/scorer" --no-sync python run_pipeline.py \
     --provider "$PROVIDER" --pipeline "$P" --results-revision "$REV" \
     --gt-dir "$WORK/gt" --work "$WORK/run" --out "$WORK/deliver" \
     --scorer-commit "$SCORER_COMMIT" --data-revision "$DATA_REV" \
     --patch-sha256 "$PATCH" --register-patch-sha256 "$REG")
done
