# ParseBench 2 metrics build

Re-scores [ParseBench 2](https://github.com/run-llama/ParseBench) pipelines from their published outputs in
[`llamaindex/ParseBench-2-results`](https://huggingface.co/datasets/llamaindex/ParseBench-2-results) and exports
per-page metrics with full, untruncated event lists. These files feed the
[`Hashir621/parsebench2-viewer`](https://huggingface.co/datasets/Hashir621/parsebench2-viewer) dataset and the
[ParseBench 2 Explorer](https://github.com/hashiromer/parsebench2-explorer).

Nothing is re-inferred. The scorer runs at a pinned ParseBench commit against pinned ground truth, using ParseBench's own
evaluator (`parse-bench evaluation run --next … --next_only`, as its `scripts/leaderboard/rescore.py` does). Two
patches are applied, and neither touches the scoring code:

- [`full_events.patch`](full_events.patch) removes the caps and truncation on the event examples each metric
  reports. Scores are unaffected.
- [`register_pipelines.patch`](register_pipelines.patch) holds ParseBench's own registration of pipelines published
  after the scorer commit, copied from upstream. It is empty (text only, skipped) at scorer `d6209da`, which registers
  every pipeline itself. The layout evaluator picks its adapter from the pipeline registry; without the registration
  an unknown pipeline falls back to a heuristic adapter (GPT-6.2 sol (medium) once scored 53.97 on Layout instead of
  the leaderboard's 53.92).

Both are rebased onto every new scorer commit; where upstream has since done what a patch did, upstream's version
wins and our hunk is dropped.

**Updating the viewer dataset?** Start with [`viewer/README.md`](viewer/README.md): it is the runbook for the whole
update (find what changed upstream, score, build, validate, upload) and holds the build code.

## Running

To score on your own machine instead of GitHub Actions, use [`viewer/score_local.sh`](viewer/score_local.sh): it runs
the same steps as the workflow.

The **Build metrics** workflow is manual only (Actions → Build metrics → Run workflow). There is no push or
schedule trigger. The inputs are:

| Input | Default | Meaning |
| --- | --- | --- |
| `pipelines` | all | Comma-separated names from [`pipelines.json`](pipelines.json) |
| `scorer_commit` | `d6209da…` | `run-llama/ParseBench` commit |
| `data_revision` | `2e95407…` | `llamaindex/ParseBench-2` ground-truth commit |
| `per_job` | 2 | pipelines scored side by side on one runner (61 pipelines: 32 jobs, 20 at a time, 34 min) |
| `leaderboard_commit` | none | ParseBench commit whose `leaderboard-v2.csv` every score must match (a final `check` job; skipped if empty) |

From the command line:

```bash
gh workflow run metrics.yml -R hashiromer/parsebench2-metrics -f pipelines=qwen3_5_4b_vllm_layout
```

Each job uploads a `metrics-batch-<n>` artifact (kept 30 days; `gh run download <run id>`) with these files per
pipeline:

- `<pipeline>.jsonl.gz`: one line per page, with every metric and all of its events
- `<pipeline>.next_score.json`: the six dimension scores
- `<pipeline>.manifest.json`: sources with SHA-256s, scores, and the scorer, ground-truth and patch versions

`pipelines.json` lists each pipeline's provider name and the results-repo commit it was published at. It includes
the pipelines added after the first delivery. It also includes `qwen3_5_4b_vllm_layout`, a control: its rescore must
reproduce the original delivery's scores exactly.
