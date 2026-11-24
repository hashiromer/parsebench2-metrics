"""Aggregates of the ParseBench 2 viewer dataset, rebuilt from the per-(pipeline, page) rows.

Inputs: metrics/scores.parquet (all pipelines), slots.parquet, pages.parquet, the leaderboard CSV, and per
pipeline: results_revision, metrics_file_sha256, bbox_frame and has_prompt. Outputs: pipelines.parquet,
metrics/page_dimension_scores.parquet, index/site.json (pipelines, counts, sources), index/dimensions/*.json,
index/parsers/*.json and index/pairwise.json.
"""
from __future__ import annotations

import csv
import math

import numpy as np
import pandas as pd

DIMS = ["text", "formatting", "tables", "charts", "layout", "forms"]
LABELS = {"text": "Text", "formatting": "Formatting", "tables": "Tables", "charts": "Charts",
          "layout": "Layout (visual grounding)", "forms": "Forms"}
CSV_COL = {"text": "Text", "formatting": "Formatting", "tables": "Tables", "charts": "Charts",
           "layout": "Visual_Grounding", "forms": "Forms"}
EVENT_COUNTS = {"text_next", "formatting_v2", "table_next", "chart_next", "forms_v2"}


def _f(x):
    """JSON-safe float: NaN/None -> None."""
    if x is None:
        return None
    try:
        if isinstance(x, float) and math.isnan(x):
            return None
    except TypeError:
        pass
    if isinstance(x, (np.floating,)):
        x = float(x)
        return None if math.isnan(x) else x
    if isinstance(x, (np.integer,)):
        return int(x)
    if x is pd.NA or x is pd.NaT:
        return None
    return x


def read_leaderboard(path: str) -> list[dict]:
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def bbox_frame(layout_pages_by_page: list) -> str | None:
    """page_units when most coordinates exceed 1, fraction otherwise, None without boxes."""
    n = big = 0
    for pages in layout_pages_by_page:
        for pg in pages or []:
            for it in pg.get("items") or []:
                b = it.get("bbox") or {}
                for k in ("x", "y", "w", "h"):
                    v = b.get(k)
                    if v is None:
                        continue
                    n += 1
                    big += v > 1
    if n == 0:
        return None
    return "page_units" if big > n / 2 else "fraction"


def slot_table(scores: pd.DataFrame, slots: pd.DataFrame) -> dict:
    """(pipeline, dimension) -> list of slot dicts with score and n_pages."""
    out = {}
    for p, g in scores.groupby("pipeline", sort=False):
        for d in DIMS:
            lst = []
            for _, s in slots[slots.dimension == d].iterrows():
                v = g.loc[g.subset == s.subset, s.metric].dropna()
                lst.append({"subset": s.subset, "metric": s.metric, "weight": float(s.weight),
                            "score": math.fsum(v) / len(v) if len(v) else None, "n_pages": int(len(v))})
            out[(p, d)] = lst
    return out


def dim_score(slots_list: list[dict]) -> float:
    return 100 * sum(s["weight"] * (s["score"] or 0.0) for s in slots_list) / sum(s["weight"] for s in slots_list)


def timing_cost(g: pd.DataFrame) -> dict:
    out = g[g.has_output]
    lat = out.latency_ms.dropna()
    cost = g.cost_usd.dropna()
    cpp = g.cost_per_page_usd.dropna()
    tok = g.total_tokens.dropna()
    return {
        "latency_ms_median": float(lat.median()) if len(lat) else None,
        "latency_ms_p90": float(np.percentile(lat, 90, method="nearest")) if len(lat) else None,
        "cost_per_page_usd_mean": math.fsum(cpp) / len(cpp) if len(cpp) else None,
        "cost_usd_total": float(math.fsum(cost)),
        "n_pages_with_cost": int(len(cost)),
        "n_pages_no_output": int((~g.has_output).sum()),
        "total_tokens_mean": float(tok.mean()) if len(tok) else None,
    }


def pipelines_table(order, lb_rows, scores, slots, meta) -> tuple[pd.DataFrame, dict, dict]:
    ranked = sorted(range(len(lb_rows)), key=lambda i: (-float(lb_rows[i]["Overall"]), i))
    lb = {lb_rows[i]["Provider"]: (rank + 1, lb_rows[i]) for rank, i in enumerate(ranked)}
    st = slot_table(scores, slots)
    groups = dict(tuple(scores.groupby("pipeline", sort=False)))
    rows, tcs = [], {}
    for p in order:
        m = meta[p]
        rank, r = lb[m["provider"]]
        row = {"pipeline": p, "provider": m["provider"], "category": r["Category"], "leaderboard_rank": rank,
               "leaderboard_overall": float(r["Overall"])}
        for d in DIMS:
            row[f"leaderboard_{d}"] = float(r[CSV_COL[d]])
        ns = m.get("next_score")  # the scorer's <pipeline>.next_score.json
        dsc = {d: (ns["dimensions"][d]["score"] * 100 if ns else dim_score(st[(p, d)])) for d in DIMS}
        row["score_overall"] = ns["next_score"] * 100 if ns else sum(dsc.values()) / len(DIMS)
        for d in DIMS:
            row[f"score_{d}"] = dsc[d]
        for d in DIMS:
            row[f"delta_{d}"] = round(dsc[d] - row[f"leaderboard_{d}"], 2)
        c = r.get("Cost_Per_Page", "")
        row["leaderboard_cost_per_page_usd"] = float(c) / 100 if c not in ("", None) else None
        tc = timing_cost(groups[p])
        tcs[p] = tc
        row.update(results_revision=m["results_revision"], metrics_file_sha256=m["metrics_file_sha256"],
                   has_cost=tc["n_pages_with_cost"] > 0, has_tokens=bool(groups[p].total_tokens.notna().any()),
                   bbox_frame=m["bbox_frame"])
        rows.append(row)
    return pd.DataFrame(rows), st, tcs


def page_dimension_scores(scores: pd.DataFrame, slots: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for d in DIMS:
        for _, s in slots[slots.dimension == d].iterrows():
            g = scores.loc[scores.subset == s.subset, ["pipeline", "test_id", "page_key", "subset", "has_output", s.metric]
                           + ([f"{s.metric}__n_{k}" for k in ("critical", "major", "minor", "events")]
                              if s.metric in EVENT_COUNTS else [])].copy()
            g = g[g[s.metric].notna()]
            g = g.rename(columns={s.metric: "score"})
            if s.metric in EVENT_COUNTS:
                g = g.rename(columns={f"{s.metric}__n_{k}": f"n_{k}" for k in ("critical", "major", "minor", "events")})
            else:
                for k in ("critical", "major", "minor", "events"):
                    g[f"n_{k}"] = pd.NA
            g["dimension"] = d
            g["metric"] = s.metric
            g["slot_weight"] = float(s.weight)
            g["n_pages_in_slot"] = g.groupby("pipeline").score.transform("size").astype("uint32")
            g["page_weight"] = g.slot_weight / g.n_pages_in_slot
            g["points_lost"] = 100 * g.page_weight * (1 - g.score)
            by = g.groupby("test_id").score
            g["rank_on_page"] = by.rank(method="min", ascending=False).astype("int32")
            g["n_parsers_scored"] = by.transform("size").astype("int32")
            g["page_mean"] = by.transform("mean")
            g["page_min"] = by.transform("min")
            g["page_max"] = by.transform("max")
            g["delta_vs_page_mean"] = g.score - g.page_mean
            parts.append(g)
    out = pd.concat(parts, ignore_index=True)
    cols = ["dimension", "pipeline", "test_id", "page_key", "subset", "metric", "score", "has_output", "slot_weight",
            "n_pages_in_slot", "page_weight", "points_lost", "rank_on_page", "n_parsers_scored", "page_mean",
            "page_min", "page_max", "delta_vs_page_mean", "n_critical", "n_major", "n_minor", "n_events"]
    out = out[cols]
    for k in ("n_critical", "n_major", "n_minor", "n_events"):
        out[k] = out[k].astype("Int64")
    return out.sort_values(["dimension", "pipeline", "score", "test_id"], kind="stable").reset_index(drop=True)
