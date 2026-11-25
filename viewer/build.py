"""Add scored pipelines to the ParseBench 2 viewer dataset, or re-score ones already in it.

  python build.py --base <dataset at the previous commit> --out <dir for changed files> \
      --leaderboard leaderboard-v2.csv --results-revision <dataset-level results pin> \
      [--scorer-revision <commit> --groundtruth-revision <revision>] \
      --add <pipeline>:<deliverable.jsonl.gz>:<next_score.json>:<results/<pipeline> dir>:<results_revision> [...] \
      --replace <same fields, for a pipeline already in the base> [...]

Per-pipeline rows come from rows.py (proved on a control pipeline by check_control.py); aggregates and index files
are rebuilt from the merged rows (proved on the base by verify_aggregates.py and verify_index.py).

--add: a pipeline new to the dataset. --replace: a pipeline whose scoring changed (a new scorer or ground truth);
all of its scoring rows (scores, events, layout, slots, page-dimension rows, metrics bundle entries) are dropped
and rebuilt from its new deliverable. What depends only on its outputs (timing, cost, tokens, bbox frame, outputs
and layout-box bundle entries) is kept as published, and asserted to be what the outputs give, unless its
results_revision changed. Pipelines neither added nor replaced keep their per-pipeline values byte-for-byte.
Replacing a pipeline with the deliverable it was published from must reproduce the base exactly (see the runbook).
Every file the build owns is written to --out; changeset.py keeps only those whose content changed.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

import aggregates as A
import index_files as I
import rows as R


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def write_parquet(df: pd.DataFrame, like: str, path: str, row_group_size: int):
    schema = pq.read_schema(like)
    t = pa.Table.from_pandas(df[schema.names], preserve_index=False).cast(schema)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    pq.write_table(t, path, compression="zstd", row_group_size=row_group_size)


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--leaderboard", required=True)
    ap.add_argument("--results-revision", required=True)
    ap.add_argument("--add", action="append", default=[])
    ap.add_argument("--replace", action="append", default=[])
    ap.add_argument("--scorer-revision", help="ParseBench commit the deliverables were scored with (site.json)")
    ap.add_argument("--groundtruth-revision", help="ParseBench-2 revision they were scored against (site.json)")
    a = ap.parse_args()
    assert a.add or a.replace, "nothing to add or replace"
    B, O = a.base, a.out
    man = json.load(open(f"{B}/viewer_manifest.json"))
    lb_rows = A.read_leaderboard(a.leaderboard)
    lb_by_provider = {r["Provider"]: r for r in lb_rows}

    base_scores = pd.read_parquet(f"{B}/metrics/scores.parquet")
    base_pl = pd.read_parquet(f"{B}/pipelines.parquet")
    site = json.load(open(f"{B}/index/site.json"))
    sp = {p["pipeline"]: p for p in site["pipelines"]}
    slots = pd.read_parquet(f"{B}/slots.parquet")
    prompts = json.load(open(f"{B}/index/prompts.json"))
    prompt_pipes = set(prompts["pipelines"]) if isinstance(prompts["pipelines"], (list, dict)) else set()

    # ---- new pipelines: per-pipeline rows -------------------------------------------------------------
    new = {}  # added and replaced pipelines: rows rebuilt from their deliverables
    base_set = set(base_pl.pipeline)
    for spec, replacing in [(x, False) for x in a.add] + [(x, True) for x in a.replace]:
        p, deliv, nsf, rawdir, rev = spec.split(":")
        assert p not in new, f"{p} given twice"
        assert (p in base_set) == replacing, f"{p}: {'not in' if replacing else 'already in'} the base"
        lines = R.load_deliverable(deliv)
        raw = R.load_raw(rawdir)
        new[p] = dict(lines=lines, raw=raw, rev=rev, deliv=deliv, next_score=json.load(open(nsf)))
        print(f"{'replace' if replacing else 'add'} {p}: {len(lines)} pages, {len(raw)} raw records")
    replaced = {p for p in new if p in base_set}
    # outputs unchanged: what depends on them only is kept as published (and checked against them)
    same_outputs = {p for p in replaced if base_pl.set_index("pipeline").at[p, "results_revision"] == new[p]["rev"]}

    # provider names (results-v2.yaml maps pipeline -> provider), as JSON in the PROVIDERS env: required for added
    # pipelines; for replaced ones it wins over the base's name, so a rename upstream is taken in a full re-run
    providers = json.loads(os.environ.get("PROVIDERS") or "{}")

    # ---- scores ---------------------------------------------------------------------------------------
    parts = [base_scores[~base_scores.pipeline.isin(replaced)]]
    for p, n in new.items():
        parts.append(R.score_rows(p, n["lines"], n["raw"], list(base_scores.columns)))
    scores = pd.concat(parts, ignore_index=True).sort_values(["subset", "test_id", "pipeline"], kind="stable")
    scores = scores.reset_index(drop=True)
    dup = scores.duplicated(["pipeline", "test_id"]).sum()
    assert dup == 0, f"{dup} duplicate (pipeline, test_id) rows"

    # ---- events, layout --------------------------------------------------------------------------------
    ev_new = {m: [] for m in R.EVENT_METRICS}
    el_new, pr_new = [], []
    for p, n in new.items():
        for m, rs in R.event_rows(p, n["lines"], man["severity_maps"]).items():
            ev_new[m] += rs
        e, pr = R.layout_rows(p, n["lines"])
        el_new += e; pr_new += pr

    # ---- pipelines (order = leaderboard rank) ----------------------------------------------------------
    for p in same_outputs:  # rows from the same outputs: the output-only columns must come out as published
        b = base_scores[base_scores.pipeline == p].sort_values(["subset", "test_id"]).reset_index(drop=True)
        g = scores[scores.pipeline == p].sort_values(["subset", "test_id"]).reset_index(drop=True)
        for c in ["test_id", "has_output", "markdown_sha256", "latency_ms", "started_at", "completed_at"] + list(R.RAW_NUMERIC):
            if c in b.columns:
                assert ((b[c].isna() & g[c].isna()) | (b[c].astype(object) == g[c].astype(object))).all(), (p, c)

    meta = {}
    for r in base_pl.to_dict("records"):
        meta[r["pipeline"]] = {"provider": r["provider"], "results_revision": r["results_revision"],
                               "metrics_file_sha256": r["metrics_file_sha256"],
                               "bbox_frame": None if pd.isna(r["bbox_frame"]) else r["bbox_frame"]}
    for p, n in new.items():
        boxes = [rec["output"].get("layout_pages") for (s, _), rec in n["raw"].items() if s == "layout"]
        meta[p] = {"provider": providers.get(p) or meta[p]["provider"], "results_revision": n["rev"],
                   "metrics_file_sha256": sha256_file(n["deliv"]), "bbox_frame": A.bbox_frame(boxes),
                   "next_score": n["next_score"]}
    all_p = list(meta)
    ranked = sorted(all_p, key=lambda p: (-float(lb_by_provider[meta[p]["provider"]]["Overall"]),
                                          lb_rows.index(lb_by_provider[meta[p]["provider"]])))
    gen, st, tcs = A.pipelines_table(ranked, lb_rows, scores, slots, meta)
    gen = gen.set_index("pipeline")
    keep = [c for c in base_pl.columns if c.startswith(("score_", "delta_")) or c in (
        "has_cost", "has_tokens", "results_revision", "metrics_file_sha256", "bbox_frame")]
    for r in base_pl.to_dict("records"):  # keep per-pipeline values of existing pipelines byte-for-byte
        if r["pipeline"] in replaced:
            if r["pipeline"] in same_outputs:  # output-only values stay as published
                for c in ("has_cost", "has_tokens", "bbox_frame"):
                    g, b = gen.at[r["pipeline"], c], r[c]
                    assert g == b or (pd.isna(g) and pd.isna(b)), (r["pipeline"], c, g, b)
            continue
        for c in keep:
            g, b = gen.at[r["pipeline"], c], r[c]
            if isinstance(b, float) and c.startswith("score_"):
                assert abs(g - b) < 1e-9, (r["pipeline"], c, g, b)
            gen.at[r["pipeline"], c] = b
    pl = gen.reset_index()[base_pl.columns]
    kept = lambda p: p in sp and p not in replaced
    slots_by = {p: ({d: sp[p]["slots"][d]["slots"] for d in A.DIMS} if kept(p) else {d: st[(p, d)] for d in A.DIMS})
                for p in ranked}
    for p in same_outputs:  # timing and cost come from the outputs only: as published, and as recomputed
        assert sp[p]["timing_cost"] == tcs[p], (p, sp[p]["timing_cost"], tcs[p])
    tc_by = {p: (sp[p]["timing_cost"] if p in sp and (p not in replaced or p in same_outputs) else tcs[p]) for p in ranked}
    for p in new:
        print(p, "rank", int(pl.set_index("pipeline").at[p, "leaderboard_rank"]),
              {c: round(float(pl.set_index("pipeline").at[p, c]), 4) for c in pl.columns if c.startswith(("score_", "delta_"))})

    # ---- page_dimension_scores -------------------------------------------------------------------------
    pds = A.page_dimension_scores(scores, slots)
    bpds = pd.read_parquet(f"{B}/metrics/page_dimension_scores.parquet")
    k = ["dimension", "pipeline", "test_id"]
    own = ["score", "has_output", "slot_weight", "n_pages_in_slot", "page_weight", "points_lost",
           "n_critical", "n_major", "n_minor", "n_events"]
    bpds = bpds[~bpds.pipeline.isin(replaced)]
    mm = pds.merge(bpds[k + own], on=k, how="left", suffixes=("", "_b"), indicator=True)
    old = mm["_merge"] == "both"
    assert old.sum() == len(bpds), "base rows missing after rebuild"
    for c in own:  # pipeline-own values of existing rows stay as published
        bc = mm.loc[old, c + "_b"]
        if pd.api.types.is_float_dtype(mm[c]):
            assert ((mm.loc[old, c] - bc).abs().fillna(0) < 1e-9).all(), c
        mm.loc[old, c] = bc
    pds = mm[pds.columns]
    pds["delta_vs_page_mean"] = pds.score - pds.page_mean
    pds = pds.sort_values(["dimension", "pipeline", "score", "test_id"], kind="stable").reset_index(drop=True)

    # ---- write parquet ---------------------------------------------------------------------------------
    write_parquet(scores, f"{B}/metrics/scores.parquet", f"{O}/metrics/scores.parquet", 20000)
    write_parquet(pds, f"{B}/metrics/page_dimension_scores.parquet", f"{O}/metrics/page_dimension_scores.parquet", 20000)
    write_parquet(pl, f"{B}/pipelines.parquet", f"{O}/pipelines.parquet", 1 << 20)
    for m, rs in ev_new.items():
        be = pd.read_parquet(f"{B}/metrics/events/{m}.parquet")
        be = be[~be.pipeline.isin(replaced)]
        df = pd.concat([be, pd.DataFrame(rs, columns=be.columns)], ignore_index=True)
        df = df.sort_values(["subset", "test_id", "pipeline", "event_index"], kind="stable")
        write_parquet(df, f"{B}/metrics/events/{m}.parquet", f"{O}/metrics/events/{m}.parquet", 100000)
    be = pd.read_parquet(f"{B}/metrics/layout_elements.parquet")
    be = be[~be.pipeline.isin(replaced)]
    df = pd.concat([be, pd.DataFrame(el_new, columns=be.columns)], ignore_index=True).sort_values(["test_id", "pipeline"], kind="stable")
    write_parquet(df, f"{B}/metrics/layout_elements.parquet", f"{O}/metrics/layout_elements.parquet", 100000)
    bp = pd.read_parquet(f"{B}/metrics/layout_predictions.parquet")
    bp = bp[~bp.pipeline.isin(replaced)]
    df = pd.concat([bp, pd.DataFrame(pr_new, columns=bp.columns)], ignore_index=True).sort_values(
        ["test_id", "pipeline", "page", "pred_index"], kind="stable")
    write_parquet(df, f"{B}/metrics/layout_predictions.parquet", f"{O}/metrics/layout_predictions.parquet", 1 << 20)

    # ---- index -----------------------------------------------------------------------------------------
    mats = {}
    for d in A.DIMS:
        dm, mats[d] = I.dimension_file(d, pds, ranked, slots_by[ranked[0]][d])
        write_text(f"{O}/index/dimensions/{d}.json", I.dumps(dm))
    write_text(f"{O}/index/pairwise.json", I.dumps(I.pairwise(ranked, mats)))
    pl_rec = {r["pipeline"]: r for r in pl.to_dict("records")}
    for p in ranked:
        write_text(f"{O}/index/parsers/{p}.json", I.dumps(I.parser_file(p, pl_rec[p], pds, slots_by[p], tc_by[p])))
    site["pipelines"] = [I.site_pipeline(pl_rec[p], (sp[p]["has_prompt"] if p in sp else p in prompt_pipes),
                                         slots_by[p], tc_by[p]) for p in ranked]
    site["sources"]["results"]["revision"] = a.results_revision
    if a.scorer_revision:
        site["sources"]["scorer"]["revision"] = a.scorer_revision
    if a.groundtruth_revision:
        site["sources"]["groundtruth"]["revision"] = a.groundtruth_revision
    site["counts"].update(pipelines=len(ranked), score_rows=len(scores), page_dimension_rows=len(pds))
    write_text(f"{O}/index/site.json", I.dumps(site))

    # ---- bundles ---------------------------------------------------------------------------------------
    ents = {p: R.bundle_entries(p, n["lines"], n["raw"], n["rev"]) for p, n in new.items()}
    for kind in ("metrics", "outputs"):
        for f in sorted(glob.glob(f"{B}/bundles/{kind}/*/*.json")):
            text = open(f, encoding="utf-8").read()
            b = json.loads(text)
            assert I.dumps(b) == text, f"non-canonical bundle {f}"
            subset, pk = f.split("/")[-2], f.split("/")[-1][:-5]
            parsers = dict(b["parsers"])
            for p in new:
                me, oe, _ = ents[p][(subset, pk)]
                e = me if kind == "metrics" else oe
                if kind == "outputs" and p in same_outputs:
                    assert parsers[p] == e, ("outputs entry changed", p, subset, pk)
                parsers[p] = e
            assert set(parsers) == set(ranked), f
            b["parsers"] = {p: parsers[p] for p in ranked}
            if kind == "outputs":
                b["source_revision"] = a.results_revision
            write_text(f"{O}/bundles/{kind}/{subset}/{pk}.json", I.dumps(b))
    for f in sorted(glob.glob(f"{B}/bundles/layout_boxes/layout/*.json")):
        text = open(f, encoding="utf-8").read()
        b = json.loads(text)
        assert I.dumps(b) == text
        pk = f.split("/")[-1][:-5]
        parsers = dict(b["parsers"])
        for p in new:
            e = ents[p][("layout", pk)][2]
            if p in same_outputs:
                assert parsers[p] == e, ("layout boxes changed", p, pk)
            parsers[p] = e
        b["parsers"] = {p: parsers[p] for p in ranked}
        b["source_revision"] = a.results_revision
        write_text(f"{O}/bundles/layout_boxes/layout/{pk}.json", I.dumps(b))

    json.dump({"ranked": ranked, "new": [p for p in new if p not in replaced], "replaced": sorted(replaced), "meta": {p: {k: v for k, v in meta[p].items() if k != "next_score"}
                                                             for p in ranked}},
              open(f"{O}/../build_state.json", "w"), indent=1)
    print("written to", O)


if __name__ == "__main__":
    main()
