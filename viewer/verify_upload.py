"""After upload.py (and again after sync_main.py): check that a dataset commit holds every local file, by hash.

  python verify_upload.py <dataset commit or branch> <dir> [<parent commit>]

Lists the commit's tree once (git blob id, or the LFS sha256) and compares every file under <dir> with it. With
<parent commit>, it also checks that the commit's parent is that commit (one new commit, history kept). Prints the
files that differ or are missing and exits 1 if there are any. Files only on the Hub are listed apart from
documents/ and images/, which the build never downloads.
"""
import hashlib
import os
import sys

from huggingface_hub import HfApi

REPO = os.environ.get("VIEWER_REPO", "Hashir621/parsebench2-viewer")
rev, d = sys.argv[1:3]
parent = sys.argv[3] if len(sys.argv) > 3 else None
api = HfApi()
c = api.list_repo_commits(REPO, repo_type="dataset", revision=rev)[:2]
print(f"{rev}: {c[0].commit_id} ({c[0].title}); parent {c[1].commit_id}")
if parent and c[1].commit_id != parent:
    raise SystemExit(f"parent is {c[1].commit_id}, expected {parent}: stop and look")
tree = {f.path: f for f in api.list_repo_tree(REPO, repo_type="dataset", revision=c[0].commit_id, recursive=True,
                                              expand=True) if hasattr(f, "blob_id")}
ok, bad, missing = 0, [], []
for root, _, files in os.walk(d):
    for n in files:
        p = os.path.join(root, n)
        rel = os.path.relpath(p, d)
        f = tree.get(rel)
        if f is None:
            missing.append(rel)
            continue
        data = open(p, "rb").read()
        eq = (f.lfs.sha256 == hashlib.sha256(data).hexdigest()) if f.lfs else \
            (f.blob_id == hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest())
        if eq:
            ok += 1
        else:
            bad.append(rel)
remote_only = sorted(p for p in tree if not os.path.exists(os.path.join(d, p))
                     and not p.startswith(("documents/", "images/")))
print(f"{ok} equal, {len(bad)} differ, {len(missing)} missing on the Hub; "
      f"only on the Hub (besides documents/, images/): {len(remote_only)}")
for x in (bad + missing)[:20]:
    print("  ", x)
for x in remote_only[:20]:
    print("   hub only:", x)
sys.exit(1 if bad or missing else 0)
