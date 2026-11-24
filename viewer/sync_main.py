"""One commit on HF main that makes its tree identical to the branch tip (no history rewrite).
Every uploaded file is checked against the branch tip's blob/LFS hash first.

  python sync_main.py <branch tip commit> <main head commit> <title> <dir with the files> [<dir> ...]"""
import hashlib, json, os, sys, time
from huggingface_hub import HfApi, CommitOperationAdd, CommitOperationDelete
R = os.environ.get("VIEWER_REPO", "Hashir621/parsebench2-viewer"); TIP, MAIN, title = sys.argv[1:4]; dirs = sys.argv[4:]
api = HfApi()
def T(rev): return {f.path: f for f in api.list_repo_tree(R, repo_type="dataset", revision=rev, recursive=True, expand=True) if hasattr(f, "blob_id")}
tip, main = T(TIP), T(MAIN)
diff = sorted(p for p in tip if p not in main or main[p].blob_id != tip[p].blob_id)
gone = sorted(p for p in main if p not in tip)
print("paths to add/update:", len(diff), "to delete:", len(gone), flush=True)
ops, bad = [], []
for p in diff:
    src = next((f"{d}/{p}" for d in dirs if os.path.exists(f"{d}/{p}")), None)
    if src is None: bad.append(("missing", p)); continue
    data = open(src, "rb").read(); f = tip[p]
    ok = (f.lfs.sha256 == hashlib.sha256(data).hexdigest()) if f.lfs else (f.blob_id == hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest())
    if not ok: bad.append(("hash", p)); continue
    ops.append(CommitOperationAdd(path_in_repo=p, path_or_fileobj=src))
ops += [CommitOperationDelete(path_in_repo=p) for p in gone]
if bad: raise SystemExit(f"not uploading: {len(bad)} problems, e.g. {bad[:5]}")
try:
    info = api.create_commit(R, repo_type="dataset", operations=ops, commit_message=title, revision="main", parent_commit=MAIN)
    print("COMMIT", info.oid)
except Exception as e:
    print("commit call failed:", str(e)[:160], flush=True)
    for _ in range(20):  # a gateway timeout can still land; parent_commit makes a retry safe
        time.sleep(60)
        head = api.list_repo_commits(R, repo_type="dataset", revision="main")[0]
        if head.title == title: print("COMMIT (landed after timeout)", head.commit_id); break
    else: print("NOT LANDED; main head", head.commit_id)
