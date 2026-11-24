"""Check scorer deliverables against the leaderboard before building: every dimension within the CSV's 2-decimal
rounding, and Overall as the leaderboard computes it (mean of the six rounded dimensions).

  python compare_scores.py <dir with <pipeline>.next_score.json, searched recursively> <leaderboard-v2.csv> <pipelines.json>
"""
import csv
import glob
import json
import os
import sys

DIR, LB, PJ = sys.argv[1:4]
COL = {"text": "Text", "formatting": "Formatting", "tables": "Tables", "charts": "Charts", "layout": "Visual_Grounding",
       "forms": "Forms"}
lb = {r["Provider"]: r for r in csv.DictReader(open(LB))}
prov = {r["pipeline"]: r["provider"] for r in json.load(open(PJ))}
found = {os.path.basename(f)[: -len(".next_score.json")]: f
         for f in glob.glob(f"{DIR}/**/*.next_score.json", recursive=True)}
bad = 0
for p in sorted(found):
    ns = json.load(open(found[p]))
    row = lb[prov[p]]
    dims = {d: ns["dimensions"][d]["score"] * 100 for d in COL}
    off = {d: (round(v, 4), row[COL[d]]) for d, v in dims.items() if abs(v - float(row[COL[d]])) > 0.005 + 1e-9}
    ov = sum(round(v, 2) for v in dims.values()) / len(COL)
    if abs(ov - float(row["Overall"])) > 0.005 + 1e-9:
        off["overall"] = (round(ov, 4), row["Overall"])
    bad += bool(off)
    print(f"{'OK  ' if not off else 'OFF '} {p}" + (f"  {off}" if off else ""))
missing = sorted(set(prov) - set(found))
print(f"{len(found)} scored, {bad} off the leaderboard; not scored: {missing if len(missing) < 10 else len(missing)}")
sys.exit(1 if bad else 0)
