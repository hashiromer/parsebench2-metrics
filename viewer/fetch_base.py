"""Assemble the dataset at a commit in --dest, reusing files already on disk and downloading only the rest.

  python fetch_base.py <dataset commit> <dest dir> [<seed dir> ...] [--all]

Only the files the build reads are fetched: the source documents, page images and ground truth (about 17,000 files
that no update changes) are skipped unless --all is given.

For every file in the dataset tree at the commit, the first seed dir holding a file with the same content hash
(git blob id, or the LFS sha256) is hard-linked into --dest (copied if links aren't possible). Everything else is
downloaded (4 workers: more trips HF rate limits). Seed dirs are usually the previous update's base and out dirs,
listed newest first. The result is checked against the tree: every file present, every hash equal.
"""
import hashlib
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor

from huggingface_hub import HfApi, hf_hub_download

REPO = os.environ.get("VIEWER_REPO", "Hashir621/parsebench2-viewer")
SKIP = ("documents/", "images/", "groundtruth/", "bundles/groundtruth/")  # never read or written by the build
args = [x for x in sys.argv[1:] if x != "--all"]
commit, dest, seeds = args[0], args[1], args[2:]
skip = () if "--all" in sys.argv else SKIP
api = HfApi()
commit = api.dataset_info(REPO, revision=commit).sha
tree = {f.path: f for f in api.list_repo_tree(REPO, repo_type="dataset", revision=commit, recursive=True, expand=True)
        if hasattr(f, "blob_id") and not f.path.startswith(skip)}


def matches(path, f):
    data = open(path, "rb").read()
    if f.lfs:
        return f.lfs.sha256 == hashlib.sha256(data).hexdigest()
    return f.blob_id == hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def place(item):
    p, f = item
    target = os.path.join(dest, p)
    if os.path.exists(target) and matches(target, f):
        return "kept"
    for s in seeds:
        src = os.path.join(s, p)
        if os.path.exists(src) and matches(src, f):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if os.path.exists(target):
                os.remove(target)
            try:
                os.link(src, target)
            except OSError:
                shutil.copy2(src, target)
            return "reused"
    return "download"


with ThreadPoolExecutor(8) as ex:
    status = dict(zip(tree, ex.map(place, tree.items())))
todo = [p for p, s in status.items() if s == "download"]
print(f"{commit[:7]}: {len(tree)} files; reused {sum(s == 'reused' for s in status.values())}, "
      f"already there {sum(s == 'kept' for s in status.values())}, downloading {len(todo)}", flush=True)


def get(p):
    for i in range(5):
        try:
            tmp = hf_hub_download(REPO, p, repo_type="dataset", revision=commit, local_dir=dest + ".dl")
            os.makedirs(os.path.dirname(os.path.join(dest, p)), exist_ok=True)
            os.replace(tmp, os.path.join(dest, p))
            return
        except Exception as e:
            if i == 4:
                raise
            print("retry", p, repr(e)[:120], flush=True)
            import time
            time.sleep(10 * 2 ** i)


with ThreadPoolExecutor(4) as ex:
    list(ex.map(get, todo))
shutil.rmtree(dest + ".dl", ignore_errors=True)
bad = [p for p, f in tree.items() if not (os.path.exists(os.path.join(dest, p)) and matches(os.path.join(dest, p), f))]
if bad:
    raise SystemExit(f"{len(bad)} files missing or different after download, e.g. {bad[:5]}")
print(f"BASE OK: {dest} equals {REPO}@{commit[:7]} ({len(tree)} files)")
