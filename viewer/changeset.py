"""List which files in --out differ from the dataset at a commit, by content hash (git blob id, or the LFS sha256).

  python changeset.py <out_dir> <dataset commit> <changeset.json>

Writes {"changed": [...], "added": [...], "removed": []}. Files identical to the dataset are left out, so the upload
(upload.py) sends only real changes. Nothing is ever removed by this toolkit.
"""
import hashlib
import json
import os
import sys

from huggingface_hub import HfApi

REPO = os.environ.get("VIEWER_REPO", "Hashir621/parsebench2-viewer")
out, commit, dest = sys.argv[1:4]

tree = {f.path: f for f in HfApi().list_repo_tree(REPO, repo_type="dataset", revision=commit, recursive=True,
                                                  expand=True) if hasattr(f, "blob_id")}
changed, added, same = [], [], 0
for root, _, files in os.walk(out):
    for name in files:
        path = os.path.relpath(os.path.join(root, name), out)
        data = open(os.path.join(root, name), "rb").read()
        f = tree.get(path)
        if f is None:
            added.append(path)
            continue
        if f.lfs:
            equal = f.lfs.sha256 == hashlib.sha256(data).hexdigest()
        else:
            equal = f.blob_id == hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        if equal:
            same += 1
        else:
            changed.append(path)
json.dump({"changed": sorted(changed), "added": sorted(added), "removed": []}, open(dest, "w"), indent=0)
print(f"changed {len(changed)}, added {len(added)}, identical (skipped) {same}")
