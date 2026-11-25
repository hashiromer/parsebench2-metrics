"""Report what changed upstream since the viewer dataset was built. Read-only: downloads a few small files and
lists repo trees; writes nothing except --report.

  python detect_changes.py [--parsebench-rev main] [--dataset-rev main] [--report changes.json]

Compares ParseBench's leaderboard/results-v2.yaml and leaderboard-v2.csv (at --parsebench-rev) with the dataset's
viewer_manifest.json and pipelines.parquet (at --dataset-rev):

  * scorer commit and ground-truth revision            -> any change means every pipeline may score differently
  * pipelines added / removed / renamed (provider)
  * per pipeline, which results/<pipeline>/<subset>.jsonl files differ (git blob ids) between the revision the
    dataset was built from and the one results-v2.yaml now names. A new revision with identical files is not a change.
  * leaderboard values that differ from the ones stored in pipelines.parquet, and CSV columns that are new or gone

The verdict at the end picks one of two paths (README.md):

  UP_TO_DATE    nothing to do
  ADD_ONLY      only new pipelines, nothing else changed: score just those and add them
  FULL_RERUN    anything else (scorer, ground truth, an existing pipeline's outputs, a provider name, leaderboard
                values): re-score every pipeline and rebuild; the leaderboard check at the end decides
  NEEDS_REVIEW  what a re-run can't settle: a pipeline removed upstream, new CSV columns, other repos

A pipeline whose results_revision is not a commit (refs/pr/<n>: an open, unmerged PR on the results repo, so its
outputs can still change) is held back: left out of everything above and listed as held back. New pipelines the
scorer commit doesn't register yet are noted (they need register_pipelines.patch).

--write-pipelines rewrites ../pipelines.json for the verdict: new pipelines added, providers as upstream names them,
upstream's pin for a pipeline whose outputs changed, the dataset's own pin kept otherwise (same files). The report's
"results_pin" is the newest of those pins: the dataset-level results revision for build.py.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import yaml
from huggingface_hub import HfApi, hf_hub_download

PARSEBENCH = "run-llama/ParseBench"
VIEWER = "Hashir621/parsebench2-viewer"
RESULTS = "llamaindex/ParseBench-2-results"
LB_COLS = {"Overall": "leaderboard_overall", "Text": "leaderboard_text", "Formatting": "leaderboard_formatting",
           "Tables": "leaderboard_tables", "Charts": "leaderboard_charts", "Visual_Grounding": "leaderboard_layout",
           "Forms": "leaderboard_forms"}
KNOWN_CSV = {"Provider", "Category", "Cost_Per_Page", "Verified", *LB_COLS}


def github_file(rev: str, path: str) -> str:
    url = f"https://raw.githubusercontent.com/{PARSEBENCH}/{rev}/{path}"
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.read().decode("utf-8")


def github_commit(rev: str) -> str:
    url = f"https://api.github.com/repos/{PARSEBENCH}/commits/{rev}"
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github.sha"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode().strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parsebench-rev", default="main")
    ap.add_argument("--dataset-rev", default="main")
    ap.add_argument("--report", default="changes.json")
    ap.add_argument("--write-pipelines", action="store_true", help="rewrite ../pipelines.json for this verdict")
    a = ap.parse_args()
    api = HfApi()

    pb_sha = github_commit(a.parsebench_rev)
    ds_sha = api.dataset_info(VIEWER, revision=a.dataset_rev).sha
    y = yaml.safe_load(github_file(pb_sha, "leaderboard/results-v2.yaml"))
    lb_text = github_file(pb_sha, "leaderboard-v2.csv")
    lb = list(csv.DictReader(io.StringIO(lb_text)))
    man = json.load(open(hf_hub_download(VIEWER, "viewer_manifest.json", repo_type="dataset", revision=ds_sha)))
    pl = pd.read_parquet(hf_hub_download(VIEWER, "pipelines.parquet", repo_type="dataset", revision=ds_sha))

    rep = {"parsebench_commit": pb_sha, "dataset_commit": ds_sha, "stop_reasons": [], "notes": []}
    stop, note = rep["stop_reasons"].append, rep["notes"].append

    # ---- scorer and ground truth ---------------------------------------------------------------------------------
    src = man["sources"]
    rep["scorer"] = {"dataset": src["scorer"]["revision"], "upstream": y.get("scored_with")}
    rep["ground_truth"] = {"dataset": src["groundtruth"]["revision"], "upstream": y.get("data_revision")}
    rescore = []  # any of these: FULL_RERUN
    full = rescore.append
    if rep["scorer"]["dataset"] != rep["scorer"]["upstream"]:
        rescore.append("scorer commit changed: every pipeline may score differently; rebase full_events.patch and "
                       "register_pipelines.patch onto the new scorer before anything is rescored")
    if rep["ground_truth"]["dataset"] != rep["ground_truth"]["upstream"]:
        rescore.append("ground-truth revision changed: every pipeline needs rescoring")
    for k in ("results_repo", "data_repo"):
        exp = RESULTS if k == "results_repo" else src["groundtruth"]["repo"]
        if y.get(k) != exp:
            stop(f"results-v2.yaml {k} is {y.get(k)!r}, expected {exp!r}")

    # ---- pipelines ----------------------------------------------------------------------------------------------------
    up = {r["pipeline"]: r for r in y["rows"]}
    have = {r["pipeline"]: r for r in pl.to_dict("records")}
    is_sha = lambda v: len(str(v)) == 40 and all(c in "0123456789abcdef" for c in str(v))
    rep["held_back"] = {p: up[p]["results_revision"] for p in up if not is_sha(up[p]["results_revision"])}
    for p, rev in sorted(rep["held_back"].items()):
        kind = "an open (unmerged) PR on the results repo" if str(rev).startswith("refs/pr/") else "not a commit"
        if p in have:
            stop(f"{p} is in the dataset but upstream now pins it to {rev!r} ({kind}): ask the owner")
        else:
            note(f"held back: {p} (results_revision {rev!r} is {kind}; its outputs can still change). It is left out "
                 "until upstream pins a commit")
    up = {p: r for p, r in up.items() if p not in rep["held_back"]}
    rep["added"] = sorted(set(up) - set(have))
    rep["removed"] = sorted(set(have) - set(up))
    rep["renamed"] = {p: {"dataset": have[p]["provider"], "upstream": up[p]["provider"]}
                      for p in set(up) & set(have) if have[p]["provider"] != up[p]["provider"]}
    rep["not_verified"] = sorted(p for p, r in up.items() if r.get("status") != "verified")
    unknown_keys = {k for r in y["rows"] for k in r} - {"pipeline", "provider", "results_revision", "run_dates", "status"}
    if unknown_keys:
        note(f"results-v2.yaml rows have new keys: {sorted(unknown_keys)}")
    if rep["removed"]:
        stop(f"pipelines removed upstream: {rep['removed']}: ask the owner whether the viewer drops them (the build "
             "can't remove a pipeline yet)")
    if rep["renamed"]:
        full(f"provider names changed: {rep['renamed']}")
    if rep["not_verified"]:
        note(f"pipelines not marked verified upstream: {rep['not_verified']}")

    # ---- results files: blob ids, per pipeline --------------------------------------------------------------------
    pairs = {p: (have[p]["results_revision"], up[p]["results_revision"]) for p in set(up) & set(have)}
    revs = {r for pr in pairs.values() for r in pr} | {up[p]["results_revision"] for p in rep["added"]}

    def tree(rev):
        files = api.list_repo_tree(RESULTS, repo_type="dataset", revision=rev, path_in_repo="results", recursive=True)
        return rev, {f.path: f.blob_id for f in files if f.path.endswith(".jsonl")}

    with ThreadPoolExecutor(6) as ex:
        T = dict(ex.map(tree, sorted(revs)))
    rep["results_changed"] = {}
    for p, (old, new) in sorted(pairs.items()):
        if old == new:
            continue
        o = {k: v for k, v in T[old].items() if k.startswith(f"results/{p}/")}
        n = {k: v for k, v in T[new].items() if k.startswith(f"results/{p}/")}
        diff = sorted(k.split("/")[-1] for k in set(o) | set(n) if o.get(k) != n.get(k))
        if diff:
            rep["results_changed"][p] = {"from": old, "to": new, "files": diff}
    # a new pipeline the scorer commit doesn't register falls back to a heuristic layout adapter (README, A2)
    scorer_parse = github_file(y.get("scored_with") or pb_sha, "src/parse_bench/inference/pipelines/parse.py")
    rep["unregistered_at_scorer"] = sorted(p for p in rep["added"] if f'"{p}"' not in scorer_parse)
    if rep["unregistered_at_scorer"]:
        note(f"not registered at the scorer commit (copy ParseBench's registration into register_pipelines.patch): "
             f"{rep['unregistered_at_scorer']}")
    rep["added_files"] = {p: sorted(k.split("/")[-1] for k in T[up[p]["results_revision"]]
                                    if k.startswith(f"results/{p}/")) for p in rep["added"]}
    for p, fs in rep["added_files"].items():
        if len(fs) != 13:
            stop(f"{p}: {len(fs)} results files at {up[p]['results_revision'][:7]} (expected 13 subsets)")
    if rep["results_changed"]:
        full(f"published outputs changed for {len(rep['results_changed'])} existing pipelines: "
             f"{sorted(rep['results_changed'])}")

    # ---- leaderboard ----------------------------------------------------------------------------------------------
    cols = set(lb[0]) if lb else set()
    if cols != KNOWN_CSV:
        stop(f"leaderboard-v2.csv columns changed: new {sorted(cols - KNOWN_CSV)}, gone {sorted(KNOWN_CSV - cols)}")
    by_provider = {r["Provider"]: r for r in lb}
    missing = sorted(r["provider"] for r in up.values() if r["provider"] not in by_provider)
    if missing:
        stop(f"providers in results-v2.yaml with no leaderboard row: {missing}")
    rep["leaderboard_changed"] = {}
    for p, r in have.items():
        row = by_provider.get(r["provider"])
        if row is None:
            continue
        d = {c: (r[k], float(row[c])) for c, k in LB_COLS.items() if c in row and abs(float(row[c]) - r[k]) > 1e-9}
        if d:
            rep["leaderboard_changed"][p] = d
    lb_only = {p: v for p, v in rep["leaderboard_changed"].items() if p not in rep["results_changed"]}
    if lb_only and rescore:
        note(f"leaderboard values changed for {len(lb_only)} pipelines, as expected after a re-score")
    elif lb_only:
        full(f"leaderboard values changed for {len(lb_only)} pipelines though scorer, ground truth and outputs did "
             "not: re-run everything; if the re-run doesn't match the leaderboard, stop and find the upstream change")

    # ---- pipelines.json for this verdict ----------------------------------------------------------------------------
    rows = [{"provider": up[p]["provider"], "pipeline": p,
             "results_revision": up[p]["results_revision"] if p in rep["results_changed"] else have[p]["results_revision"]}
            for p in have if p in up]
    rows += [{"provider": up[p]["provider"], "pipeline": p, "results_revision": up[p]["results_revision"]}
             for p in rep["added"]]
    rep["pipelines"] = rows
    pins = {r["results_revision"] for r in rows}
    rep["results_pin"] = next((c.commit_id for c in api.list_repo_commits(RESULTS, repo_type="dataset")
                               if c.commit_id in pins), None)

    # ---- verdict ----------------------------------------------------------------------------------------------------
    rep["rescore_reasons"] = rescore
    if rep["stop_reasons"]:
        rep["verdict"] = "NEEDS_REVIEW"
        rep["stop_reasons"] += rescore
    elif rescore:
        rep["verdict"] = "FULL_RERUN"
    elif rep["added"]:
        rep["verdict"] = "ADD_ONLY"
    else:
        rep["verdict"] = "UP_TO_DATE"
    json.dump(rep, open(a.report, "w"), indent=1, default=str)
    if a.write_pipelines and rep["verdict"] in ("ADD_ONLY", "FULL_RERUN"):
        import os
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pipelines.json")
        old = json.load(open(path))
        order = [r["pipeline"] for r in old] + [p for p in rep["added"]]
        by = {r["pipeline"]: r for r in rows}
        out = [by[p] for p in order if p in by]
        open(path, "w").write("[\n" + ",\n".join("  " + json.dumps(r, ensure_ascii=False) for r in out) + "\n]\n")
        print(f"wrote pipelines.json: {len(by)} pipelines")

    print(f"ParseBench {pb_sha[:7]} vs dataset {ds_sha[:7]}")
    print(f"  added:   {rep['added'] or '-'}")
    print(f"  removed: {rep['removed'] or '-'}")
    print(f"  outputs changed: {sorted(rep['results_changed']) or '-'}")
    lc = sorted(rep["leaderboard_changed"])
    print(f"  leaderboard changed: {(lc if len(lc) <= 10 else f'{len(lc)} pipelines') or '-'}")
    for s in rep["notes"]:
        print("  note:", s)
    for s in rep["stop_reasons"]:
        print("  STOP:", s)
    if rep["verdict"] == "FULL_RERUN":
        for s in rescore:
            print("  FULL_RERUN:", s)
    print("  results pin:", rep["results_pin"])
    print("VERDICT", rep["verdict"], f"(details in {a.report})")
    sys.exit(0)


if __name__ == "__main__":
    main()
