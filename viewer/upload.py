"""Upload the change set (changeset.json) from --out to the dataset as commits on --branch.

  python upload.py <out_dir> <changeset.json> <branch> <parent_commit> <message> [--parts N]
With --parts, paths are uploaded in N sequential commits: bundles first, then everything else last, so the
final commit is the first complete state."""
import json, os, sys, time
from huggingface_hub import HfApi, CommitOperationAdd

out, csf, branch, parent, msg = sys.argv[1:6]
parts = int(sys.argv[sys.argv.index("--parts") + 1]) if "--parts" in sys.argv else 1
REPO = os.environ.get("VIEWER_REPO", "Hashir621/parsebench2-viewer")
cs = json.load(open(csf))
paths = sorted(cs["changed"] + cs["added"])
assert not cs["removed"]
api = HfApi()
bundles = [p for p in paths if p.startswith("bundles/")]
rest = [p for p in paths if not p.startswith("bundles/")]
if parts == 1:
    groups = [paths]
else:
    n = parts - 1
    groups = [bundles[i::n] for i in range(n)] + [rest]
for i, g in enumerate(groups, 1):
    title = msg if parts == 1 else f"{msg} (part {i} of {len(groups)})"
    ops = [CommitOperationAdd(path_in_repo=p, path_or_fileobj=f"{out}/{p}") for p in g]
    t = time.time()
    for attempt in (1, 2):
        try:
            info = api.create_commit(REPO, repo_type="dataset", operations=ops,
                                     commit_message=title, revision=branch, parent_commit=parent)
            oid = info.oid
            break
        except Exception as e:  # a gateway timeout can still land: check the branch head before retrying
            print(f"{title}: attempt {attempt} failed: {str(e)[:120]}", flush=True)
            time.sleep(30)
            head = api.list_repo_commits(REPO, repo_type="dataset", revision=branch)[0]
            if head.title == title:
                oid = head.commit_id
                print(f"{title}: landed despite the error", flush=True)
                break
            if attempt == 2:
                raise SystemExit(f"{title}: failed twice; stopping (branch head {head.commit_id})")
    parent = oid
    print(f"{title}: {len(ops)} files -> {oid} in {time.time() - t:.0f}s", flush=True)
print("FINAL", parent)
