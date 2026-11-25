"""List which files in --out differ from the dataset at a commit, by content.

  python changeset.py <out_dir> <dataset commit> <changeset.json>                  # compare with the Hub's tree
  python changeset.py <out_dir> <dataset commit> <changeset.json> --local <base>   # compare with a local copy

Writes {"changed": [...], "added": [...], "removed": []}. Files identical to the dataset are left out, so the upload
(upload.py) sends only real changes. Nothing is ever removed by this toolkit.

--local compares with a directory holding the dataset at that commit (the base fetch_base.py checked, e.g. stage0 of
build_passes.sh). It takes seconds; the Hub listing of all ~26,000 files takes many minutes. A file that is the same
inode as the base's (a hard link) is identical by definition. Files that are in the base but not in out_dir are
reported as a count: after build_passes.sh the final tree holds every file, so that count should be 0.
"""
import filecmp
import hashlib
import json
import os
import sys

REPO = os.environ.get("VIEWER_REPO", "Hashir621/parsebench2-viewer")
out, commit, dest = sys.argv[1:4]
local = sys.argv[sys.argv.index("--local") + 1] if "--local" in sys.argv else None

if local:
    def equal_to_dataset(path, full):
        b = os.path.join(local, path)
        if not os.path.exists(b):
            return None
        return os.path.samefile(full, b) or filecmp.cmp(full, b, shallow=False)
else:
    from huggingface_hub import HfApi
    tree = {f.path: f for f in HfApi().list_repo_tree(REPO, repo_type="dataset", revision=commit, recursive=True,
                                                      expand=True) if hasattr(f, "blob_id")}

    def equal_to_dataset(path, full):
        f = tree.get(path)
        if f is None:
            return None
        data = open(full, "rb").read()
        if f.lfs:
            return f.lfs.sha256 == hashlib.sha256(data).hexdigest()
        return f.blob_id == hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()

changed, added, same, size = [], [], 0, 0
for root, _, files in os.walk(out):
    for name in files:
        full = os.path.join(root, name)
        path = os.path.relpath(full, out)
        eq = equal_to_dataset(path, full)
        if eq is None:
            added.append(path)
        elif eq:
            same += 1
            continue
        else:
            changed.append(path)
        size += os.path.getsize(full)
json.dump({"changed": sorted(changed), "added": sorted(added), "removed": []}, open(dest, "w"), indent=0)
msg = f"changed {len(changed)}, added {len(added)}, identical (skipped) {same}; upload {size / 1e9:.2f} GB"
if local:
    absent = sum(1 for r, _, fs in os.walk(local) for n in fs
                 if not os.path.exists(os.path.join(out, os.path.relpath(os.path.join(r, n), local))))
    msg += f"; in the base but not in out_dir: {absent}"
print(msg)
