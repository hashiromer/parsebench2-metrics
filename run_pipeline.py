"""Score one ParseBench 2 pipeline from its published outputs and export full-event metrics.

Produces the same three files as the original metrics delivery:
  <pipeline>.jsonl.gz        one line per page with every metric and its full (uncapped) event list
  <pipeline>.next_score.json the six dimension scores
  <pipeline>.manifest.json   sources, hashes and the scores

Scoring uses ParseBench's own evaluator (`parse-bench evaluation run --next ... --next_only`, as
scripts/leaderboard/rescore.py does), with full_events.patch applied so event lists are not truncated.
Nothing is re-inferred: the parser outputs come from llamaindex/ParseBench-2-results at a pinned commit.

Run it with the ParseBench checkout's environment:
  uv run --project <scorer> python run_pipeline.py --provider ... --pipeline ... --results-revision ...
"""
import argparse
import gzip
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

from huggingface_hub import snapshot_download

RESULTS_REPO = "llamaindex/ParseBench-2-results"
DATA_REPO = "llamaindex/ParseBench-2"
SUBSETS = ["text", "headings", "strikeout", "underline", "tables", "complex_table_headers", "financial_tables",
           "insurance_filing_tables", "dense_tables", "charts", "layout", "forms", "fillable_forms"]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def retry(fn, attempts: int = 6):
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - the Hub drops connections under load
            if i == attempts - 1:
                raise
            wait = 15 * 2 ** i
            print(f"retry in {wait}s: {type(e).__name__}: {str(e)[:200]}", flush=True)
            time.sleep(wait)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True)
    ap.add_argument("--pipeline", required=True)
    ap.add_argument("--results-revision", required=True)
    ap.add_argument("--gt-dir", type=Path, required=True, help="ground truth snapshot of llamaindex/ParseBench-2")
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--scorer-commit", required=True)
    ap.add_argument("--data-revision", required=True)
    ap.add_argument("--patch-sha256", required=True)
    a = ap.parse_args()
    gt_dir, work, out = a.gt_dir.resolve(), a.work.resolve(), a.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    hf_dir = work / "hf"
    retry(lambda: snapshot_download(RESULTS_REPO, repo_type="dataset", revision=a.results_revision,
                                    allow_patterns=[f"results/{a.pipeline}/*", "tools/*"], local_dir=str(hf_dir),
                                    max_workers=4))
    results_dir = hf_dir / "results" / a.pipeline
    files = []
    for subset in SUBSETS:
        p = results_dir / f"{subset}.jsonl"
        if not p.exists():
            raise SystemExit(f"missing results/{a.pipeline}/{subset}.jsonl at {a.results_revision}")
        name = f"results/{a.pipeline}/{subset}.jsonl"
        files.append({"subset": subset, "path": name,
                      "url": f"https://huggingface.co/datasets/{RESULTS_REPO}/resolve/{a.results_revision}/{name}",
                      "sha256": sha256(p), "bytes": p.stat().st_size})

    output_root = work / "output" / "parsebench-2"
    subprocess.run([sys.executable, str(hf_dir / "tools" / "explode.py"), "--benchmark", "parsebench-2",
                    str(results_dir), str(output_root)], check=True)
    sources = {s: {"output_dir": str(output_root / s / a.pipeline), "test_cases_dir": str(gt_dir / s)}
               for s in SUBSETS}
    missing = [s for s, src in sources.items() if not Path(src["output_dir"]).is_dir()]
    if missing:
        raise SystemExit(f"explode produced no folder for {missing}")
    report_dir = work / "reports"
    cmd = [sys.executable, "-m", "parse_bench.cli", "evaluation", "run", str(output_root),
           "--next", json.dumps(sources), "--next_only", "--report_dir", str(report_dir), "--force", "True",
           "--export_csv", "False", "--export_rule_csv", "False", "--export_markdown", "False",
           "--export_html", "False"]
    subprocess.run(cmd, check=True)
    score = json.loads((report_dir / "_next_score.json").read_text())

    deliver = out / f"{a.pipeline}.jsonl.gz"
    pages = events = 0
    with gzip.open(deliver, "wt", encoding="utf-8") as gz:
        for subset in SUBSETS:
            sub = output_root / subset / a.pipeline
            rep = json.loads((sub / "_evaluation_report.json").read_text())
            md_shas: dict[str, str] = {}
            for rf in sub.rglob("*.result.json"):
                rec = json.loads(rf.read_text())
                md = (rec.get("output") or {}).get("markdown") or ""
                md_shas[(rec.get("request") or {}).get("example_id")] = hashlib.sha256(md.encode("utf-8")).hexdigest()
            for r in rep.get("per_example_results", []):
                for m in r.get("metrics", []):
                    events += len((m.get("metadata") or {}).get("event_examples") or [])
                line = {"pipeline": a.pipeline, "subset": subset, "test_id": r["test_id"],
                        "example_id": r.get("example_id"),
                        "markdown_sha256": md_shas.get(r.get("example_id")) or md_shas.get(r["test_id"]),
                        "success": r.get("success"), "error": r.get("error"),
                        "metrics": r.get("metrics", []), "stats": r.get("stats", [])}
                gz.write(json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n")
                pages += 1
    (out / f"{a.pipeline}.next_score.json").write_text(json.dumps(score, indent=2))
    entry = {
        "provider": a.provider, "pipeline": a.pipeline,
        "results_repo": RESULTS_REPO, "results_revision": a.results_revision, "files": files,
        "pages": pages, "event_examples": events, "score_seconds": round(time.time() - t0),
        "deliverable": deliver.name, "deliverable_sha256": sha256(deliver),
        "next_score": score.get("next_score"),
        "dimensions": {k: v.get("score") for k, v in score.get("dimensions", {}).items()},
        "parsebench_commit": a.scorer_commit, "ground_truth_repo": DATA_REPO,
        "ground_truth_revision": a.data_revision, "patch_sha256": a.patch_sha256,
    }
    (out / f"{a.pipeline}.manifest.json").write_text(json.dumps(entry, indent=2))
    print(json.dumps({k: entry[k] for k in ("pipeline", "pages", "next_score", "dimensions")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
