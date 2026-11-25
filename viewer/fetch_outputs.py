"""Get the published outputs (results/<pipeline>/*.jsonl) of pipelines at their pins in pipelines.json.

  python fetch_outputs.py <dest> [--pipelines a,b] [--seed <dir with results/> ...]

Lists each distinct results_revision's tree once, then for every pipeline: keeps files already in <dest> with the
right hash, hard-links them from a seed (a previous update's results dir) if one has them, and downloads only the
rest, one snapshot per revision, with retries. Ends by checking every file's hash and that each pipeline has 13.
More than ~4-6 download workers trips HF rate limits (HTTP 429). All 61 pipelines are about 4.5 GB.
"""
import argparse
import collections
import hashlib
import json
import os
import sys
import time

from huggingface_hub import HfApi, snapshot_download

RESULTS = "llamaindex/ParseBench-2-results"
HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("dest")
ap.add_argument("--pipelines", default="")
ap.add_argument("--seed", action="append", default=[])
a = ap.parse_args()

rows = json.load(open(f"{HERE}/../pipelines.json"))
if a.pipelines:
    only = set(a.pipelines.split(","))
    assert only <= {r["pipeline"] for r in rows}, only - {r["pipeline"] for r in rows}
    rows = [r for r in rows if r["pipeline"] in only]
api = HfApi()
trees = {}
for rev in sorted({r["results_revision"] for r in rows}):
    trees[rev] = {f.path: f for f in api.list_repo_tree(RESULTS, repo_type="dataset", revision=rev,
                                                       path_in_repo="results", recursive=True)
                  if f.path.endswith(".jsonl")}


def good(path, f):
    if not os.path.isfile(path):
        return False
    if f.lfs:
        if os.path.getsize(path) != f.lfs.size:
            return False
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 22), b""):
                h.update(chunk)
        return h.hexdigest() == f.lfs.sha256
    data = open(path, "rb").read()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == f.blob_id


want = {}  # path -> (revision, tree entry)
for r in rows:
    fs = {k: v for k, v in trees[r["results_revision"]].items() if k.startswith(f"results/{r['pipeline']}/")}
    assert len(fs) == 13, f"{r['pipeline']}: {len(fs)} files at {r['results_revision'][:7]}"
    want.update({k: (r["results_revision"], v) for k, v in fs.items()})

todo = collections.defaultdict(list)
kept = linked = 0
for path, (rev, f) in sorted(want.items()):
    dst = os.path.join(a.dest, path)
    if good(dst, f):
        kept += 1
        continue
    for s in a.seed:
        src = os.path.join(s, path)
        if good(src, f):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if os.path.exists(dst):
                os.unlink(dst)
            os.link(src, dst)
            linked += 1
            break
    else:
        todo[rev].append(path)
print(f"{len(want)} files: {kept} already there, {linked} linked from seeds, "
      f"{sum(map(len, todo.values()))} to download", flush=True)

for rev, paths in todo.items():
    for i in range(6):
        try:
            snapshot_download(RESULTS, repo_type="dataset", revision=rev, allow_patterns=paths, local_dir=a.dest,
                              max_workers=4)
            break
        except Exception as e:
            if i == 5:
                raise
            print("retry", i, repr(e)[:200], flush=True)
            time.sleep(15 * 2 ** i)

bad = [p for p, (rev, f) in want.items() if not good(os.path.join(a.dest, p), f)]
print(f"{len(rows)} pipelines, {len(want) - len(bad)} of {len(want)} files verified")
sys.exit(1 if bad else 0)
