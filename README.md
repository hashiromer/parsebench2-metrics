# ParseBench 2 metrics build

Re-scores [ParseBench 2](https://github.com/run-llama/ParseBench) pipelines from their published outputs in
[`llamaindex/ParseBench-2-results`](https://huggingface.co/datasets/llamaindex/ParseBench-2-results) and exports
per-page metrics with full, untruncated event lists. These files feed the
[`Hashir621/parsebench2-viewer`](https://huggingface.co/datasets/Hashir621/parsebench2-viewer) dataset and the
[ParseBench 2 Explorer](https://github.com/hashiromer/parsebench2-explorer).

Nothing is re-inferred. The scorer runs at a pinned ParseBench commit against pinned ground truth, using ParseBench's own
evaluator (`parse-bench evaluation run --next … --next_only`, as its `scripts/leaderboard/rescore.py` does). The
only change is [`full_events.patch`](full_events.patch), which removes the caps and truncation on the event examples
that each metric reports. Scores are unaffected.

## Running

The **Build metrics** workflow is manual only (Actions → Build metrics → Run workflow). There is no push or
schedule trigger. The inputs are:

| Input | Default | Meaning |
| --- | --- | --- |
| `pipelines` | all | Comma-separated names from [`pipelines.json`](pipelines.json) |
| `scorer_commit` | `d7efdbc…` | `run-llama/ParseBench` commit |
| `data_revision` | `2d9a234…` | `llamaindex/ParseBench-2` ground-truth commit |

From the command line:

```bash
gh workflow run metrics.yml -R hashiromer/parsebench2-metrics -f pipelines=qwen3_5_4b_vllm_layout
```

Each job uploads a `metrics-<pipeline>` artifact with these files:

- `<pipeline>.jsonl.gz`: one line per page, with every metric and all of its events
- `<pipeline>.next_score.json`: the six dimension scores
- `<pipeline>.manifest.json`: sources with SHA-256s, scores, and the scorer, ground-truth and patch versions

`pipelines.json` lists each pipeline's provider name and the results-repo commit it was published at. It includes
the pipelines added after the first delivery. It also includes `qwen3_5_4b_vllm_layout`, a control: its rescore must
reproduce the original delivery's scores exactly.
