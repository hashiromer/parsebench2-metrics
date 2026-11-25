"""Per-pipeline rows for the ParseBench 2 viewer dataset.

Everything here is a pure function of one pipeline's scorer deliverable (`<pipeline>.jsonl.gz` from
hashiromer/parsebench2-metrics) and its raw results files (llamaindex/ParseBench-2-results). The functions
reproduce the rows the original build wrote; `check_control.py` proves that on a pipeline already in the
dataset before any new pipeline is added.
"""
from __future__ import annotations

import glob
import gzip
import hashlib
import json
import os

import pandas as pd

RESULTS_REPO = "llamaindex/ParseBench-2-results"
EVENT_METRICS = ["text_next", "formatting_v2", "table_next", "chart_next", "forms_v2"]
MAPPED = {"text_next", "chart_next"}  # events without their own severity


def page_key(test_id: str) -> str:
    return hashlib.sha256(test_id.encode("utf-8")).hexdigest()[:16]


def load_deliverable(path: str) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f]


def load_raw(results_dir: str) -> dict[tuple[str, str], dict]:
    """(subset, example_id) -> raw record."""
    out = {}
    for f in sorted(glob.glob(os.path.join(results_dir, "*.jsonl"))):
        subset = os.path.basename(f)[: -len(".jsonl")]
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                r = json.loads(line)
                out[(subset, r["request"]["example_id"])] = r
    return out


def _metric(line: dict, name: str) -> dict | None:
    for m in line["metrics"]:
        if m["metric_name"] == name:
            return m
    return None


def _ts(v):
    return pd.Timestamp(v, tz="UTC") if v else pd.NaT


RAW_NUMERIC = {  # scores column -> raw_output key
    "cost_usd": "cost_usd",
    "cost_per_page_usd": "cost_per_page_usd",
    "num_pages": "num_pages",
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
    "thinking_tokens": "thinking_tokens",
    "total_tokens": "total_tokens",
    "cache_read_tokens": "cache_read_tokens",
    "cache_write_tokens": "cache_write_tokens",
    "credits_used": "credits_used",
    "processing_duration_ms": "processing_duration_ms",
}


def score_rows(pipeline: str, lines: list[dict], raw: dict, columns: list[str]) -> pd.DataFrame:
    rows = []
    for ln in lines:
        rec = raw.get((ln["subset"], ln["test_id"])) or raw.get((ln["subset"], ln.get("example_id")))
        row = {c: None for c in columns}
        row.update(pipeline=pipeline, test_id=ln["test_id"], page_key=page_key(ln["test_id"]), subset=ln["subset"],
                   has_output=rec is not None, markdown_sha256=ln["markdown_sha256"] if rec is not None else None)
        for m in ln["metrics"]:
            if m["metric_name"] in row:
                row[m["metric_name"]] = m["value"]
        for name in EVENT_METRICS:
            m = _metric(ln, name)
            if m is None:
                continue
            md = m.get("metadata") or {}
            ev = md.get("events") or {}
            row[f"{name}__proportional"] = md.get("proportional")
            row[f"{name}__floor"] = md.get("floor")
            row[f"{name}__n_critical"] = ev.get("critical")
            row[f"{name}__n_major"] = ev.get("major")
            row[f"{name}__n_minor"] = ev.get("minor")
            row[f"{name}__n_events"] = len(md.get("event_examples") or [])
        lm = _metric(ln, "layout_grounding_pass_rate")
        if lm is not None:
            md = lm.get("metadata") or {}
            row["layout__passed"] = md.get("passed")
            row["layout__total"] = md.get("total")
            row["layout__no_predictions"] = bool(md.get("no_predictions", False))
        if rec is not None:
            row["latency_ms"] = rec.get("latency_in_ms")
            row["started_at"] = _ts(rec.get("started_at"))
            row["completed_at"] = _ts(rec.get("completed_at"))
            ro = rec.get("raw_output") or {}
            for col, key in RAW_NUMERIC.items():  # in raw_output, or at the record's top level (processing_duration_ms)
                v = ro.get(key) if ro.get(key) is not None else rec.get(key)
                if col in row and v is not None:
                    row[col] = v
        rows.append(row)
    return pd.DataFrame(rows, columns=columns)


def event_rows(pipeline: str, lines: list[dict], severity_maps: dict) -> dict[str, list[dict]]:
    out = {m: [] for m in EVENT_METRICS}
    for ln in lines:
        for name in EVENT_METRICS:
            m = _metric(ln, name)
            if m is None:
                continue
            for i, e in enumerate((m.get("metadata") or {}).get("event_examples") or []):
                kind = e.get("kind", e.get("type"))
                if name in MAPPED:
                    sev, src = severity_maps[name].get(kind), "mapped"
                else:
                    sev, src = e.get("severity"), "stored"
                out[name].append(dict(
                    pipeline=pipeline, test_id=ln["test_id"], page_key=page_key(ln["test_id"]), subset=ln["subset"],
                    metric=name, event_index=i, kind=kind, severity=sev, severity_source=src,
                    charged=e.get("charged"), reference=e.get("reference", e.get("expected")), output=e.get("output"),
                    field=e.get("field")))
    return out


def layout_rows(pipeline: str, lines: list[dict]) -> tuple[list[dict], list[dict]]:
    els, preds = [], []
    for ln in lines:
        lm = _metric(ln, "layout_grounding_pass_rate")
        if lm is None:
            continue
        md = lm.get("metadata") or {}
        base = dict(pipeline=pipeline, test_id=ln["test_id"], page_key=page_key(ln["test_id"]))
        for e in md.get("element_results") or []:
            els.append({**base, **{k: e.get(k) for k in ("element_id", "page", "gt_class", "L", "A", "token_f1",
                                                          "tightness", "score", "weight", "group_id", "why")}})
        for p in md.get("prediction_results") or []:
            preds.append({**base, **{k: p.get(k) for k in ("page", "pred_index", "kind", "reason", "penalty")}})
    return els, preds


def bundle_entries(pipeline: str, lines: list[dict], raw: dict, results_revision: str):
    """page_key -> (metrics entry, outputs entry, layout_boxes entry or None)."""
    out = {}
    for ln in lines:
        rec = raw.get((ln["subset"], ln["test_id"])) or raw.get((ln["subset"], ln.get("example_id")))
        ro = (rec or {}).get("raw_output") or {}
        metrics = {"markdown_sha256": ln["markdown_sha256"] if rec else None,
                   "latency_ms": rec.get("latency_in_ms") if rec else None,
                   "cost_usd": ro.get("cost_usd"), "metrics": ln["metrics"]}
        url = f"https://huggingface.co/datasets/{RESULTS_REPO}/resolve/{results_revision}/results/{pipeline}/{ln['subset']}.jsonl"
        outputs = {"markdown": (rec or {}).get("output", {}).get("markdown") if rec else None, "source_url": url}
        boxes = (rec or {}).get("output", {}).get("layout_pages") if rec else None
        out[(ln["subset"], page_key(ln["test_id"]))] = (metrics, outputs, boxes)
    return out
