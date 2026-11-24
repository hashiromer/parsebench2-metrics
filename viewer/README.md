# Viewer dataset build: runbook

This folder turns scored pipelines into the [`Hashir621/parsebench2-viewer`](https://huggingface.co/datasets/Hashir621/parsebench2-viewer)
dataset that the [ParseBench 2 Explorer](https://github.com/hashiromer/parsebench2-explorer) reads.

**It is meant to be run by hand, by a person or an AI working with that person, never on a schedule.** Upstream
(ParseBench, its results repo, its leaderboard) changes often and in ways nobody here controls. Each update therefore
starts by finding out what changed (step 1). The code covers the common case, adding new pipelines, and stops on
everything else. Read this whole file before running anything, then read the code you are about to run. The scripts
encode rules that were reverse-engineered from the published data, and the checks are what keep an update honest.

## Rules

These come from the dataset's owner and hold for every update:

- **Don't change any number that didn't change upstream.** Existing pipelines' rows, slot scores, timing and bundle
  entries are kept byte-for-byte (`build.py` asserts this); `validate.py` re-checks it.
- **No force-pushes, no deleting repos or branches, no rewriting HF history.** Every dataset change is a new commit
  with `parent_commit` set; the previous commit must stay available for rollback.
- **Don't post anything on run-llama repos.**
- **If something is unclear, or would take much longer than planned, stop and ask.** Never "decide" a scoring question.
- Run everything locally (this VM). Don't use GitHub Actions for this.

## The update, step by step

Use a work directory on the main disk, e.g. `~/match_parsebench/work` (`/tmp` is a 12 GB in-memory drive and runs
out). Python deps: `uv run --with pandas --with pyarrow --with pyyaml --with huggingface_hub python …`.

### 1. Find out what changed (read-only)

```bash
python detect_changes.py --report work/changes.json
```

It compares ParseBench `main` (`leaderboard/results-v2.yaml`, `leaderboard-v2.csv`) with the dataset's `main` and
ends with a verdict:

| Verdict | Meaning | What to do |
| --- | --- | --- |
| `UP_TO_DATE` | nothing to do | stop |
| `ADD_ONLY` | only new pipelines; scorer, ground truth, existing outputs and leaderboard values unchanged | steps 2–9 |
| `NEEDS_REVIEW` | anything else; each `STOP:` line says why | stop and ask, see "When the verdict is NEEDS_REVIEW" |

Pin the ParseBench commit and dataset commit it printed; use them for every later step.

### 2. Register and score the new pipelines

For each new pipeline:

1. Add it to [`../pipelines.json`](../pipelines.json) (provider exactly as in `leaderboard-v2.csv`, and the
   `results_revision` from `results-v2.yaml`).
2. Check ParseBench for the pipeline's registration in `src/parse_bench/inference/pipelines/parse.py` (and its
   provider). If the scorer commit predates it, add ParseBench's own registration to
   [`../register_pipelines.patch`](../register_pipelines.patch), copied from upstream, not written here. Without it
   the layout evaluator falls back to a heuristic adapter and Layout can be off (53.97 vs 53.92 once).
3. Score locally, plus the control `qwen3_5_4b_vllm_layout`:

   ```bash
   viewer/score_local.sh work/score <scorer commit> <data revision> <new pipeline> qwen3_5_4b_vllm_layout
   ```

   The scorer commit and data revision are the ones in `results-v2.yaml` (`scored_with`, `data_revision`). Output:
   `work/score/deliver/<pipeline>.{jsonl.gz,next_score.json,manifest.json}`. The control's `next_score.json` must
   equal the dataset's `score_*` for it exactly.

Each new pipeline's six `next_score` values should match `leaderboard-v2.csv` to its 2-decimal rounding. If one
doesn't, stop: it usually means a missing registration (above) or an upstream scorer change.

### 3. Get the base and the new pipelines' outputs

```bash
python fetch_base.py <dataset commit> work/<update>/base work/<previous update>/out work/<previous update>/base
hf download llamaindex/ParseBench-2-results --repo-type dataset --revision <results_revision> \
    --include "results/<pipeline>/*" --local-dir work/<update>/results --max-workers 4
```

`fetch_base.py` hard-links every file that is already on disk with the right content hash and downloads only the
rest, then checks the whole tree. It skips what the build never reads (source documents, page images, ground truth:
about 17,000 files), so with the previous update's directories as seeds it usually downloads nothing. Without
seeds it fetches about 9,300 files (3.5 GB); more than ~6 workers trips HF rate limits (HTTP 429).

Do the second command for each new pipeline, and for the control (`--local-dir work/results-ctrl`, at the control's
`results_revision` from `pipelines.json`). Check that the newest results revision only *adds* files to the one the
dataset is pinned at (compare the two trees); it becomes the dataset's results pin.

### 4. Prove the code still reproduces the base

Before adding anything, prove the code matches the published data. These rebuild existing data and must pass:

```bash
python check_control.py work/base qwen3_5_4b_vllm_layout work/score/deliver/qwen3_5_4b_vllm_layout.jsonl.gz \
    work/results-ctrl/results/qwen3_5_4b_vllm_layout <its results_revision>     # per-pipeline rows, events, bundles
python verify_aggregates.py work/base work/leaderboard-v2.csv                  # pipelines table, slots, page scores
python verify_index.py work/base                                               # index/*.json
```

If one fails, upstream or the dataset format moved. Find out why before going on; don't patch around it.

### 5. Build

```bash
PROVIDERS='{"<pipeline>": "<provider>"}' python build.py --base work/base --out work/out \
    --leaderboard work/leaderboard-v2.csv --results-revision <newest results revision> \
    --add <pipeline>:<deliverable.jsonl.gz>:<next_score.json>:work/results/results/<pipeline>:<its results_revision>
```

It writes only files whose content changes to `work/out`, plus `work/build_state.json`. Rank is by leaderboard
Overall.

### 6. Validate

```bash
python validate.py work/base work/out work/leaderboard-v2.csv <pipeline>[,<pipeline>]   # writes work/checks.json
```

Every check must pass. The leaderboard check compares each dimension within the CSV's 2-decimal rounding, and
Overall as **the mean of the six rounded dimension scores**. That is how ParseBench's `scripts/leaderboard/apply.py`
computes it, although its README says "unweighted mean". Nine providers differ by more than rounding if you use the
unrounded mean; that is expected.

### 7. Manifest, validation report, README

Write a `finalize_cfg.json` (see the last update's in "History" below for the shape) and run:

```bash
python finalize.py work/base work/out work/build_state.json work/checks.json work/finalize_cfg.json
```

### 8. Upload: one commit on a branch, then sync `main`

```bash
python changeset.py work/out <dataset commit> work/changeset.json      # only files that really differ
python upload.py work/out work/changeset.json <branch> <branch head> "<message>"           # try one commit
python upload.py … --parts 6                                                                 # only if that fails
python sync_main.py <branch tip> <main head> "Sync main to <branch> @ <tip>" work/out work/base
```

A single commit sends about 3.5 GB, because the bundles are regular git files, and may answer **504 yet land**.
`upload.py` checks the branch head before retrying, and so must you: never re-upload blindly. After the upload,
download a sample of the files back and compare their hashes. `sync_main.py` makes one extra commit so `main`'s tree
equals the branch tip (no history rewrite).

### 9. Point the explorer at the new commit

In `parsebench2-explorer/src/lib/config.ts`, update `DATA_COMMIT`, the results and leaderboard revisions, and
`PARSER_COUNT`, plus the count in its README. Push a branch first: Workers Builds gives it a preview at
`<branch>-parsebench2-explorer.hashir.workers.dev`. Check it, then merge to `main`, which deploys production. Build
logs: `cf builds list` / `cf builds logs <id>` (the Builds API takes only the `cf` user sign-in, not account tokens).

## When the verdict is NEEDS_REVIEW

Each case needs a decision from the dataset's owner, and usually new code. Handle them with the same pattern: prove
the change on existing data first, as `check_control.py` does.

- **Scorer commit or ground truth changed.** Every pipeline may score differently. Check that both patches still apply
  and touch no scoring code. Rescore everything, with the control first. This is a full rebuild, not an increment.
- **An existing pipeline's outputs changed** (`results_changed` lists the subsets). `build.py` only adds pipelines.
  Replacing one means dropping its rows everywhere (scores, events, layout, bundles) and re-adding it. Write and
  prove that before using it.
- **A pipeline was removed or renamed upstream.** Ask whether the viewer should drop it, keep it, or rename it.
- **Leaderboard values changed for pipelines whose outputs didn't.** Something in ParseBench's scoring or rounding
  moved. Find the upstream commit that did it before trusting either side.
- **CSV columns changed** (a new dimension, for example). The dataset format itself has to change; plan it with the
  owner.

## What the code encodes

These rules were reverse-engineered from the published data; the verify scripts prove them on the base:

- `page_key` = first 16 hex characters of `sha256(test_id)`.
- Leaderboard rank: Overall descending, ties in CSV order.
- p90 latency uses the `nearest` percentile method; cost totals and per-page means use `math.fsum`; `page_std` uses ddof 1.
- Pairwise wins and losses use a tie tolerance of 1e-9.
- Sort orders: `page_dimension_scores` by (dimension, pipeline, score, test_id); parser rows by (score ascending,
  points_lost descending, test_id).
- The Layout dimension's label is "Layout (visual grounding)"; `layout__no_predictions` comes from the metric's
  `no_predictions` metadata.
- Dimension scores are the scorer's own `next_score`.
- Severity: `text_next` and `chart_next` events carry none, so it comes from the manifest's `severity_maps`. Chart
  `legend lost` maps to null, which is expected.
- About 205 of 826 published slot scores differ in the last binary digit from every formula tried. Existing pipelines
  keep their published values; new ones use `fsum / n`. This is recorded in the manifest's deviations.

## Files

| File | Does |
| --- | --- |
| `detect_changes.py` | step 1: what changed upstream, and a verdict (read-only) |
| `score_local.sh` | step 2: score pipelines on this machine, as `.github/workflows/metrics.yml` does |
| `rows.py` | per-pipeline rows: scores, events, layout, bundle entries |
| `aggregates.py`, `index_files.py` | pipelines table, slots, page-dimension scores; `index/*.json` |
| `check_control.py`, `verify_aggregates.py`, `verify_index.py` | step 4: prove the code on existing data |
| `build.py` | step 5: incremental build (adds pipelines only) |
| `validate.py`, `finalize.py` | steps 6–7 |
| `fetch_base.py` | step 3: the dataset at a commit, reusing files on disk |
| `changeset.py`, `upload.py`, `sync_main.py` | step 8 |

## History

- **2026-11-23**, 59 → 60 parsers: added `liteparse_ocria_nano`. Dataset `8fc443f` on branch `update-59-parsers`,
  then `main` synced at `9a042c2`. Results `e7cd5e1`, leaderboard `c8f7798`. Control GPT-6.2 sol (medium): PASS. All
  checks passed; 9,250 files changed. The single-commit upload answered 504 and had landed.
- **2026-11-24**: `detect_changes.py` reports a new upstream pipeline, `ocria_1`, not yet added.
