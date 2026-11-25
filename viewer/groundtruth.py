"""Ground-truth files of the ParseBench 2 viewer dataset, rebuilt from a llamaindex/ParseBench-2 revision.

  python groundtruth.py <ground truth dir (<subset>.jsonl files)> <its revision> <base> <out>

Writes groundtruth/groundtruth.parquet, bundles/groundtruth/<subset>/<page_key>.json and pages.parquet (only its
document_url changes: it pins the ground-truth revision; the documents themselves are the same files). The rules
were reverse-engineered from the published dataset; prove them first by running this at the base's own revision
and comparing (`--check`), which must report no differences.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import urllib.parse

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import index_files as I
from build import write_parquet, write_text
from rows import page_key

GT_REPO = "llamaindex/ParseBench-2"


def document_url(revision: str, document: str) -> str:
    return f"https://huggingface.co/datasets/{GT_REPO}/resolve/{revision}/{urllib.parse.quote(document)}"


def load_gt(gt_dir: str) -> dict[str, dict]:
    """test_id -> the record from <subset>.jsonl."""
    out = {}
    for f in sorted(glob.glob(os.path.join(gt_dir, "*.jsonl"))):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                assert r["id"] not in out, r["id"]
                out[r["id"]] = r
    return out


def as_obj(x):
    """metadata / test_rules as objects. ParseBench-2 stores them as JSON strings, except some records corrected
    in v2.08 (2e95407), which hold the objects themselves."""
    return json.loads(x) if isinstance(x, str) else x


def as_text(x) -> str:
    """test_rules as the JSON string the bundles have always held (the encoding of every string record)."""
    return x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)


def bundle(r: dict, revision: str) -> dict:
    b = dict(r)
    b["metadata"] = as_obj(r["metadata"])
    b["test_rules"] = as_text(r["test_rules"])
    b["page_key"] = page_key(r["id"])
    b["document_url"] = document_url(revision, r["document"])
    return b


def table(gt: dict[str, dict]) -> pd.DataFrame:
    rows = [{"test_id": t, "page_key": page_key(t), "subset": r["subset"], "dimension": r["dimension"],
             "tags": list(r["tags"]), "expected_markdown": r["expected_markdown"],
             "metadata_json": I.dumps(as_obj(r["metadata"])),
             "test_rules_json": I.dumps(as_obj(r["test_rules"])),
             "n_rules": len(as_obj(r["test_rules"]))} for t, r in gt.items()]
    return pd.DataFrame(rows).sort_values("test_id", kind="stable").reset_index(drop=True)


def pages(base: str, gt: dict[str, dict], revision: str) -> pd.DataFrame:
    pg = pd.read_parquet(f"{base}/pages.parquet")
    assert set(pg.test_id) == set(gt), "pages and ground truth list different test cases"
    assert (pg.document_path == pg.test_id.map(lambda t: gt[t]["document"])).all(), "a document path moved"
    pg["document_url"] = [document_url(revision, d) for d in pg.document_path]
    return pg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("gt_dir")
    ap.add_argument("revision")
    ap.add_argument("base")
    ap.add_argument("out")
    ap.add_argument("--check", action="store_true", help="compare with the base instead of writing (proof mode)")
    a = ap.parse_args()
    gt = load_gt(a.gt_dir)
    gtab, pg = table(gt), pages(a.base, gt, a.revision)
    bundles = {f"bundles/groundtruth/{r['subset']}/{page_key(t)}.json": I.dumps(bundle(r, a.revision)) for t, r in gt.items()}
    print(f"{len(gt)} test cases")
    if a.check:
        bad = 0
        for rel, text in sorted(bundles.items()):
            p = f"{a.base}/{rel}"
            if not os.path.exists(p):
                continue
            if open(p, encoding="utf-8").read() != text:
                bad += 1
                if bad <= 5:
                    print("differs:", rel)
        on_disk = len(glob.glob(f"{a.base}/bundles/groundtruth/*/*.json"))
        print(f"bundles: {on_disk} on disk compared, {bad} differ; {len(bundles) - on_disk} not on disk")
        for name, mine in (("groundtruth/groundtruth.parquet", gtab), ("pages.parquet", pg)):
            p = f"{a.base}/{name}"
            if os.path.exists(p):
                theirs = pq.read_table(p)
                same = pa.Table.from_pandas(mine[theirs.schema.names], preserve_index=False).cast(theirs.schema).equals(theirs)
                print(f"{name}: {'same content' if same else 'DIFFERS'}")
            else:
                print(f"{name}: not in base")
        return
    write_parquet(gtab, f"{a.base}/groundtruth/groundtruth.parquet", f"{a.out}/groundtruth/groundtruth.parquet", 500)
    write_parquet(pg, f"{a.base}/pages.parquet", f"{a.out}/pages.parquet", 1 << 20)
    for rel, text in bundles.items():
        write_text(f"{a.out}/{rel}", text)
    print("written to", a.out)


if __name__ == "__main__":
    main()
