"""index/*.json for static sites, rebuilt from pipelines + page_dimension_scores."""
from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd

from aggregates import DIMS, LABELS, _f

DIM_NOTE = ("scores[i][j] is page i, pipeline j (0-1, as published; null = metric not applicable). points_lost[i][j] "
            "is how many points page i cost pipeline j's dimension score.")
PARSER_NOTE = "rows are sorted lowest score first (ties: larger points_lost first)"
PAIR_NOTE = ("wins[a][b] = pages where pipeline a scored higher than b (both scored); mean_diff_points[a][b] = mean of "
             "(a - b) * 100 over those shared pages.")
TIE_EPS = 1e-9  # scores closer than this are a tie
PARSER_COLS = ["page_key", "subset", "score", "points_lost", "rank_on_page", "page_mean", "n_critical", "n_major", "n_minor"]


def dumps(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def dimension_file(d: str, pds: pd.DataFrame, order: list[str], slot_rows: list[dict]) -> tuple[dict, dict]:
    g = pds[pds.dimension == d]
    pages = g.drop_duplicates("test_id")[["test_id", "page_key", "subset"]].sort_values("test_id")
    pi = {t: i for i, t in enumerate(pages.test_id)}
    pj = {p: j for j, p in enumerate(order)}
    n, m = len(pages), len(order)
    S = [[None] * m for _ in range(n)]
    L = [[None] * m for _ in range(n)]
    C = [[None] * m for _ in range(n)]
    for r in g[["test_id", "pipeline", "score", "points_lost", "n_critical"]].itertuples(index=False):
        i, j = pi[r.test_id], pj[r.pipeline]
        S[i][j] = float(r.score)
        L[i][j] = float(r.points_lost)
        C[i][j] = None if pd.isna(r.n_critical) else int(r.n_critical)
    by = g.groupby("test_id").score
    mean, mn, mx, sd, cnt = by.mean(), by.min(), by.max(), by.std(), by.size()
    t = list(pages.test_id)
    out = {
        "dimension": d, "label": LABELS[d],
        "slots": [{k: s[k] for k in ("subset", "metric", "weight", "n_pages")} for s in slot_rows],
        "pipelines": order, "pages": list(pages.page_key), "test_ids": t, "subsets": list(pages.subset),
        "page_mean": [_f(mean[x]) for x in t], "page_min": [_f(mn[x]) for x in t], "page_max": [_f(mx[x]) for x in t],
        "page_std": [_f(sd[x]) for x in t], "n_parsers_scored": [int(cnt[x]) for x in t],
        "note": DIM_NOTE, "scores": S, "points_lost": L, "n_critical": C,
    }
    return out, {"S": S}


def pairwise(order: list[str], mats: dict) -> dict:
    out = {"pipelines": order, "note": PAIR_NOTE, "dimensions": {}}
    for d in DIMS:
        S = np.array([[np.nan if v is None else v for v in row] for row in mats[d]["S"]], dtype=float)
        m = S.shape[1]
        wins = [[0] * m for _ in range(m)]
        ties = [[0] * m for _ in range(m)]
        md = [[None] * m for _ in range(m)]
        for a in range(m):
            for b in range(m):
                both = ~np.isnan(S[:, a]) & ~np.isnan(S[:, b])
                x, y = S[both, a], S[both, b]
                wins[a][b] = int((x - y > TIE_EPS).sum())
                ties[a][b] = int((np.abs(x - y) <= TIE_EPS).sum())
                md[a][b] = float(np.mean((x - y) * 100)) if both.any() else None
        out["dimensions"][d] = {"wins": wins, "ties": ties, "mean_diff_points": md}
    return out


def parser_file(p: str, prow: dict, pds: pd.DataFrame, slots: dict, tc: dict) -> dict:
    dims = {}
    g = pds[pds.pipeline == p]
    for d in DIMS:
        x = g[g.dimension == d].copy()
        x = x.sort_values(["score", "points_lost", "test_id"], ascending=[True, False, True], kind="stable")
        rows = [[r.page_key, r.subset, float(r.score), float(r.points_lost), int(r.rank_on_page), float(r.page_mean),
                 None if pd.isna(r.n_critical) else int(r.n_critical), None if pd.isna(r.n_major) else int(r.n_major),
                 None if pd.isna(r.n_minor) else int(r.n_minor)] for r in x.itertuples(index=False)]
        dims[d] = {"score": prow[f"score_{d}"], "leaderboard": prow[f"leaderboard_{d}"],
                   "points_lost_total": float(x.points_lost.sum()), "slots": slots[d], "columns": PARSER_COLS,
                   "note": PARSER_NOTE, "rows": rows}
    return {"pipeline": p, "provider": prow["provider"], "category": prow["category"],
            "leaderboard_rank": int(prow["leaderboard_rank"]), "score_overall": prow["score_overall"],
            "dimensions": dims, "timing_cost": tc}


def site_pipeline(prow: dict, has_prompt: bool, slots: dict, tc: dict) -> dict:
    out = {k: _f(v) for k, v in prow.items()}
    out["leaderboard_rank"] = int(prow["leaderboard_rank"])
    out["has_cost"] = bool(prow["has_cost"]); out["has_tokens"] = bool(prow["has_tokens"])
    out["has_prompt"] = has_prompt
    out["slots"] = {d: {"slots": slots[d]} for d in DIMS}
    out["timing_cost"] = tc
    return out
