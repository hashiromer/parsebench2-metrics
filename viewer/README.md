# Viewer dataset build: runbook

This folder turns scored pipelines into the [`Hashir621/parsebench2-viewer`](https://huggingface.co/datasets/Hashir621/parsebench2-viewer)
dataset that the [ParseBench 2 Explorer](https://github.com/hashiromer/parsebench2-explorer) reads.

**It is meant to be run by hand, by a person or an AI working with that person, never on a schedule.** Upstream
(ParseBench, its results repo, its leaderboard) changes often and in ways nobody here controls. Each update therefore
starts by finding out what changed (step 1), which picks one of two paths: add new pipelines, or re-run everything.
Read this whole file before running anything (the "Pitfalls" section especially), then read the code you are about
to run. The scripts encode rules that were reverse-engineered from the published data, and the checks are what keep
an update honest.

## Rules

These come from the dataset's owner and hold for every update:

- **Don't change any number that didn't change upstream.** When only pipelines are added, existing pipelines' rows,
  slot scores, timing and bundle entries are kept byte-for-byte (`build.py` asserts this; `validate.py` re-checks
  it). A full re-run recomputes everything; it is proved to reproduce the dataset when nothing changed upstream (see
  History, 2026-11-25), and its leaderboard check must pass for every pipeline.
- **No force-pushes, no deleting repos or branches, no rewriting HF history.** Every dataset change is a new commit
  with `parent_commit` set; the previous commit must stay available for rollback.
- **Don't post anything on run-llama repos.**
- **If something is unclear, or would take much longer than planned, stop and ask.** Never "decide" a scoring question.
- **A full re-run needs the owner's go-ahead** (it changes almost every file), and so does every upload.
- The build, checks and upload run locally (the maintainer's VM). Scoring may run either locally
  (`score_local.sh`) or on GitHub Actions (the "Build metrics" workflow of this public repo, several pipelines per
  job, all jobs in parallel), which is much faster for a full re-run. Its outputs are artifacts kept 30 days;
  download them with `gh run download`.
- Our changes to ParseBench (`full_events.patch`, `register_pipelines.patch`) are rebased onto every new scorer
  commit. Where upstream has since added or fixed something a patch did, upstream's version wins and our hunk is
  dropped (an emptied patch is skipped); ours stays only for what upstream lacks (uncapped events).

## Before you start: a fresh machine or session

Everything an update needs is in this repo and the explorer's; nothing lives only in an earlier chat. A new session
needs both repos, the tools below and the owner's logins.

**Tokens never go into a repo.** No token, key or password is written into either repo, a script, a commit message,
a config file, a command line or a chat. Each tool keeps its own login in the owner's home directory (`hf` in
`~/.cache/huggingface/`, `gh` in `~/.config/gh/`, `cf` in its own profile), and the scripts use that login
(`HfApi()` with no token argument). Before using a tool, check whether it is signed in. If it isn't, stop, tell the
owner which command to run, and let them run it themselves in their own terminal. An agent never asks for a token,
pastes one or types one in. The scoring workflow uses no secrets: everything it reads is public.

| Access | Needed for | Check | If not signed in, the owner runs |
| --- | --- | --- | --- |
| Hugging Face, write access to `Hashir621/parsebench2-viewer` | the upload only ("Finish", step 2); every read is public | `hf auth whoami` | `hf auth login` (a token with write access to that repo) |
| GitHub as `hashiromer` | `gh workflow run` / `gh run download` (artifacts need a login, even on a public repo), pushing both repos, cloning the explorer (private) | `gh auth status` | `gh auth login` |
| Cloudflare (optional) | only reading the explorer's build logs; a push to `main` deploys by itself | `env -u CLOUDFLARE_API_TOKEN cf auth whoami` | `cf auth login` |

On the maintainer's VM, `CLOUDFLARE_API_TOKEN` in the environment is an old token that was deleted. Run `cf` as
`env -u CLOUDFLARE_API_TOKEN cf …` so it uses the login.

Tools (install what is missing; none needs a token):

| Tool | For | Install |
| --- | --- | --- |
| `git`, `python3` | everything | system packages |
| [`uv`](https://docs.astral.sh/uv/) | every Python script (`uv run --with …` brings its own packages) | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `hf` | Hugging Face login | `uv tool install huggingface_hub` |
| `gh` | GitHub | [cli.github.com](https://cli.github.com/) |
| Node 22+ with `pnpm` | the explorer (`pnpm install`, `pnpm run build:vinext`, `node scripts/check-licenses.mjs`) | [nvm](https://github.com/nvm-sh/nvm), then `npm i -g pnpm` |
| Playwright Chromium | the page check | `cd viewer/site_check && npm i --no-save playwright && npx playwright install chromium` |
| `cf` (optional) | Cloudflare build logs | `npm i -g cf` |

Folder layout (the commands in this file assume it; `work/` is outside both repos and never committed):

```
<any folder>/
  parsebench2-metrics/    git clone https://github.com/hashiromer/parsebench2-metrics
  parsebench2-explorer/   gh repo clone hashiromer/parsebench2-explorer
  scorer/                 git clone https://github.com/run-llama/ParseBench   (only to rebase the patches, F1)
  work/                   changes.json and each update's work dirs: <any folder>/work, not viewer/work
  work/seed/              optional: the dataset tree of the last update, for fetch_base.py to hard-link from
```

`work/seed` exists only on the maintainer's VM (about 10 GB). Without it, `fetch_base.py` and `fetch_outputs.py`
download what they need: slower, same result. Keep `work/` on the main disk, not `/tmp`. A full re-run's build peaks
at about 6 GB of memory (passes of 10 pipelines), and the 2026-11-24/25 runs left about 54 GB of work data: keep at
least that much disk free.

Each update's `finalize.py` settings are kept in git: [`finalize_cfg.example.json`](finalize_cfg.example.json) is
the 2026-11-24 full re-run's. Copy it, then update every field (see "Finish", step 1).

## Two paths

Every update takes one of two paths, picked by `detect_changes.py` (step 1). There is deliberately no third,
"change only what changed" path: upstream moves faster than anyone can review small changes one by one, so anything
beyond added pipelines is a full re-run, and the leaderboard check at its end decides whether it is right.

| Verdict | When | What to do |
| --- | --- | --- |
| `UP_TO_DATE` | nothing changed (held-back pipelines don't count) | stop |
| `ADD_ONLY` | only new pipelines; scorer, ground truth, outputs, providers and leaderboard values of the others unchanged | "Add only" below: score just the new ones |
| `FULL_RERUN` | anything else: a new scorer or ground truth, an existing pipeline's outputs, a provider renamed, leaderboard values moved | "Full re-run" below: re-score and rebuild every pipeline |
| `NEEDS_REVIEW` | what a re-run can't settle: a pipeline removed upstream, new CSV columns, a different results or ground-truth repo | stop and ask the owner |

A pipeline whose `results_revision` is not a commit (`refs/pr/<n>`: an open, unmerged PR on the results repo, so its
outputs can still change) is **held back**: left out of the verdict and of `pipelines.json` until upstream pins a
commit. Tencent Hy4 Flash was held back this way on 2026-11-25.

## 1. Find out what changed (read-only)

```bash
uv run --with pandas --with pyarrow --with pyyaml --with huggingface_hub python detect_changes.py \
    --report work/changes.json --write-pipelines
```

It compares ParseBench `main` (`leaderboard/results-v2.yaml`, `leaderboard-v2.csv`) with the dataset's `main`, prints
the verdict, and writes the report (pin its ParseBench and dataset commits for every later step). With
`--write-pipelines` it also rewrites [`../pipelines.json`](../pipelines.json) for an `ADD_ONLY` or `FULL_RERUN`
verdict: new pipelines added, providers as upstream names them, upstream's pin for a pipeline whose outputs changed,
and the dataset's own pin kept where the files are the same. It notes new pipelines the scorer commit doesn't
register: they need [`../register_pipelines.patch`](../register_pipelines.patch) (step A2).

## Add only

### A1. pipelines.json

`detect_changes.py --write-pipelines` has added the new pipelines. Check the diff (`git diff ../pipelines.json`).

### A2. Registration

If step 1 noted a new pipeline as not registered at the scorer commit, copy ParseBench's own registration (from the
upstream commit that added it, in `src/parse_bench/inference/pipelines/parse.py`) into `register_pipelines.patch`.
Copy it; don't write it. Without it the layout evaluator falls back to a heuristic adapter and Layout can be off
(53.97 vs 53.92 once).

### A3. Score the new pipelines, plus the control

```bash
viewer/score_local.sh work/score <scorer commit> <data revision> <new pipeline> qwen3_5_4b_vllm_layout
```

or the "Build metrics" workflow with `-f pipelines=<new>,qwen3_5_4b_vllm_layout -f leaderboard_commit=<ParseBench
commit>`. The scorer commit and data revision are `scored_with` and `data_revision` in `results-v2.yaml`. The
control's `next_score.json` must equal the dataset's `score_*` for it exactly, and each new pipeline's six values
must match `leaderboard-v2.csv` to its 2-decimal rounding (`compare_scores.py`). If one doesn't, stop: it usually
means a missing registration (A2) or an upstream scorer change.

### A4. Base and outputs

```bash
python fetch_base.py <dataset commit> work/<update>/base <previous update's out> <previous update's base>
python fetch_outputs.py work/<update>/results --pipelines <new>,qwen3_5_4b_vllm_layout --seed <previous results dir>
```

Both hard-link what is already on disk with the right hash and download only the rest, then check every file. With
the previous update's directories as seeds they usually download nothing.

### A5. Prove the code still reproduces the base

```bash
python check_control.py work/base qwen3_5_4b_vllm_layout work/score/deliver/qwen3_5_4b_vllm_layout.jsonl.gz \
    work/results/results/qwen3_5_4b_vllm_layout <its results_revision>     # per-pipeline rows, events, bundles
python verify_aggregates.py work/base work/leaderboard-v2.csv              # pipelines table, slots, page scores
python verify_index.py work/base                                           # index/*.json
```

If one fails, upstream or the dataset format moved. Find out why; don't patch around it.

### A6. Build and validate

```bash
PROVIDERS='{"<pipeline>": "<provider>"}' python build.py --base work/base --out work/out \
    --leaderboard work/leaderboard-v2.csv --results-revision <results pin from the report> \
    --add <pipeline>:<deliverable.jsonl.gz>:<next_score.json>:work/results/results/<pipeline>:<its results_revision>
python validate.py work/base work/out work/leaderboard-v2.csv <pipeline>[,<pipeline>]   # writes work/checks.json
```

`build.py` writes only files whose content changes, plus `work/build_state.json`; existing pipelines keep every
value byte-for-byte (it asserts this, and validate re-checks it). Every check must pass (validate exits 1 if not).

Then "Finish" below, with `work/base` and `work/out`.

## Full re-run

Get the owner's go-ahead first (it uploads ~4 GB and changes almost every file), then:

### F1. Rebase the patches (only if the scorer commit changed)

Apply each patch on a checkout of the new scorer commit; where it doesn't apply cleanly use `git apply --3way` and
resolve. Check whether upstream already has the change: a registration can apply cleanly yet duplicate one upstream
added (at run time: "Pipeline already registered"). Drop what upstream has; keep ours only for what upstream lacks
(today: the uncapped events). Regenerate with `git diff <scorer commit> HEAD > ../<patch>`; an emptied patch keeps
only its explanatory text and is skipped. Register any new pipeline the scorer lacks (A2).

### F2. Score everything on GitHub Actions

```bash
gh workflow run metrics.yml -R hashiromer/parsebench2-metrics -f scorer_commit=<scored_with> \
    -f data_revision=<data_revision> -f leaderboard_commit=<ParseBench commit from the report>
```

Push `pipelines.json` and the patches first: the workflow uses the repo's `main`. It scores two pipelines per job, 20
jobs at a time (61 pipelines: 32 jobs, 34 min), and its `check` job compares every pipeline with the leaderboard;
read its summary before going on. A batch that failed can be re-run alone with `-f pipelines=a,b`.

### F3. Build and validate: one command

```bash
SEEDS=work/seed/dataset RESULTS_SEEDS=work/seed/results viewer/full_rerun.sh work/<update> work/changes.json <run id>
```

`work/seed` is the one local copy kept between updates (10 GB): `dataset/` (the dataset at its last commit, ground
truth included), `results/` (every pipeline's published outputs) and `gt/` (ParseBench-2 ground truth, with a
`.done-<revision>` marker). Everything else in `work/` is deleted once an update is uploaded and checked: it can all be
fetched again. With the seed, the scripts hard-link instead of downloading; add `GT_DIR=work/seed/gt` when the ground
truth didn't change. After an update, replace `seed/dataset` with the uploaded tree (and `seed/results`, `seed/gt` if
they changed), check it with `fetch_base.py <new commit> work/seed/dataset --gt`, then delete the work dir.

[`full_rerun.sh`](full_rerun.sh) fetches the base with its ground-truth files, the leaderboard (dropping rows of
held-back pipelines), the run's deliverables (and checks them against the leaderboard again), every pipeline's
outputs, and the new ground truth; rebuilds the ground-truth tables and bundles (`groundtruth.py`); builds in passes
of 10 (`build_passes.sh`, about 3.5 min and 5.7 GB per pass); and validates every pipeline. It ends with
`VALIDATED TREE: <dir>`. Re-running it reuses what is already on disk. `GT_DIR` and `DELIVER` point it at a ground
truth or deliverables already downloaded.

Then "Finish" below, with `work/<update>/stage0` as the base and the validated tree as out.

## Finish (both paths)

### 1. Manifest, validation report, README

Write a `work/<update>/finalize_cfg.json`: copy [`finalize_cfg.example.json`](finalize_cfg.example.json) (the
2026-11-24 full re-run's) and update every field: the four revisions, `old_count` (parsers before this update),
`metrics_build_note`, `control`, `replace_proof`, `deviation_replace` / `deviations_add` (changes to the manifest's
"deviations_from_build_spec"), and `readme_replace`, `[old, new]` pairs of the dataset README's text (each old text
must be in the current README; take it from the live dataset, not from the example). Then run:

```bash
python finalize.py <base> <out> <build_state.json> <checks.json> work/<update>/finalize_cfg.json
```

In `readme_replace`, write `{overall_off_count}` and `{overall_off_range}` where the README says for how many parsers
the unrounded mean differs from the published Overall: finalize fills them in. The leaderboard computes Overall as
**the mean of the six rounded dimension scores** (`scripts/leaderboard/apply.py`, although its README says
"unweighted mean"), so a dozen or so parsers differ by more than rounding from the unrounded mean; that is expected.

### 2. Upload: one commit on a branch, then sync `main`

```bash
python changeset.py <out> <dataset commit> work/changeset.json --local <base>       # seconds; without --local, minutes
python upload.py <out> work/changeset.json <branch> <dataset commit> "<message>"      # one commit
python verify_upload.py <branch> <out> <dataset commit>                              # every file, by hash; parent
python sync_main.py <branch tip> <main head> "Sync main to <branch> @ <tip>" <out> <base>
python verify_upload.py main <out> <main head before the sync>
```

Check that `main` is still the dataset commit you built on before uploading. A single commit sends ~4 GB on a full
re-run (the JSON bundles are plain git files, not Xet, and list parsers in rank order, so most change) and may answer
**504 yet land**: `upload.py` checks the branch head before retrying, and so must you; never re-upload blindly.
`--parts 6` splits it only if a single commit fails. `sync_main.py` makes one extra commit so `main`'s tree equals
the branch tip (no history rewrite).

### 3. Point the explorer at the new commit

In `parsebench2-explorer/src/lib/config.ts`, update `DATA_COMMIT`, `DATA_UPDATED_AT` (that commit's time, UTC ISO
8601: the site shows it as "updated 3 minutes ago" in the visitor's time zone), the scorer, ground-truth, results and
leaderboard revisions in `SOURCES`, and `PARSER_COUNT` (plus the count in its README). New providers may need a rule in
`src/lib/brands.ts` for their logo.

The leaderboard filters parsers by Open source (upstream category `Open Weight`) / Closed source, and open source by
Free / Commercial. Every open-source parser needs a license entry in the explorer's `src/lib/licenses.ts`. Run
`node scripts/check-licenses.mjs` in the explorer (it reads the dataset at `DATA_COMMIT`; pass another commit as its
argument). It lists every open-source parser without an entry, with candidate Hugging Face models and their declared
licenses, and exits 1 if any is missing. For each one:

1. Find the model the pipeline runs: the candidate whose name matches, or the model id in the provider file under
   `src/parse_bench/inference/providers/parse/` of ParseBench. Raw models only: the license is the weights' license,
   never that of an app or library that runs them, even when the usual app's is stricter (MinerU2.5-Pro is "free":
   its weights are Apache-2.0, although the MinerU app adds revenue limits).
2. Open `https://huggingface.co/<model>`: the `license:` in the card's metadata, then the `LICENSE` file itself.
   `license: other` or a custom name always means reading the file.
3. `"free"` if you can do anything with it, business use included, with nothing beyond attribution: Apache-2.0,
   MIT, BSD, OpenMDW. Everything else is `"commercial"`, with a short `note` on the condition: copyleft (AGPL),
   non-commercial only (CC-BY-NC), a revenue or funding cap (modified OpenRAIL-M: Datalab, OCRia), a region or user
   cap (Tencent Hunyuan), no use in a competing service.
4. Add `pipeline: hf("<model>", "<license name>", "<free|commercial>", "<note>")`. Ask the user when a license is
   unclear.

A parser with no entry still shows under Open source but under neither Free nor Commercial.

Check pages with [`site_check/check.mjs`](site_check/check.mjs) (headless Chromium; add the new parsers' pages and
some re-scored ones to `site_check/pages.json`): first on a local build (`pnpm run build:vinext && npx vite preview
--port 4173`, then `BASE=http://localhost:4173`), because on a branch preview (`*.workers.dev`) pages that fetch the
dataset fail with a CORS error (Hugging Face doesn't allow that origin). Then merge to `main`, which deploys
production in about a minute, and run it again with `BASE=https://parsebench.hashiromer.com`. It must end with
"0 with problems"; deliberate bad links (kind `bad`) are listed apart and get the app's not-found view.

## Pitfalls (read before running anything)

Each of these cost time in an earlier update.

- **Each shell command starts fresh.** Variables set in one command (`W=…`) are gone in the next; a script run with
  `$W` unset gets `/stage0`. Set them in the same command or use full paths.
- **The VM's shell is zsh.** `$c:` in a loop is a zsh modifier, not a colon; run such loops with `bash -c`.
  `pkill -f <pattern>` matches its own shell and kills it (exit 144); stop a server by its port's PID
  (`ss -ltnp | grep :4173`).
- **`ls` on the VM is aliased to a decorated listing** (sizes, icons): never pipe it into another command. Use
  `find … -printf '%f\n'` or `command ls -1`.
- **`/tmp` is a 12 GB in-memory drive.** Keep work dirs on the main disk (`~/match_parsebench/work`). A full re-run
  needs about 10 GB beyond the outputs (4.5 GB) and deliverables.
- **Hard-linked trees.** The stage trees share files with the base. Anything that writes into one must write a new
  file and rename it over the old one (finalize.py does), never write through it, or the base changes too.
- **Don't build 61 pipelines in one `build.py` call**: it runs out of memory. `build_passes.sh` does passes of 10.
- **The Hub listing of the dataset with `expand=True` takes many minutes** (26,000 files). Use
  `changeset.py --local` against the verified base; `verify_upload.py` lists once per commit.
- **More than ~4–6 download workers trips HF rate limits** (HTTP 429). The scripts use 4 and retry with back-off; the
  workflow downloads the ground truth once into the Actions cache instead of in every job.
- **Ground truth can change type.** v2.08 stores `metadata`/`test_rules` of 11 corrected pages as a JSON object
  instead of a string; `groundtruth.py` encodes them like the others (`json.dumps(…, ensure_ascii=False)`).
- **Some fields sit at a record's top level**, not in `raw_output` (`processing_duration_ms` for
  `llamaparse_cost_effective`); `rows.py` reads both.
- **Nulls compare unequal.** NaN vs None for pages with no output made validate report 45 false mismatches; compare
  with nulls normalized, and compare parquet tables with Arrow equality, not pandas element-wise.
- **Upstream's pins vs the dataset's.** `results-v2.yaml` may name a newer results commit for a pipeline whose files
  are identical; `detect_changes.py` compares file hashes, not commit ids, and keeps the dataset's pin.
- **Workers Builds status and logs:** `cf builds list --worker <script tag>` takes the Worker's ID, not its name
  (with the name it returns nothing); the explorer's is `efa3b80d46024203804724103c5f5b9d` (`cf workers scripts
  search --name parsebench2-explorer`). Then `cf builds get <build uuid>` and `cf builds logs <build uuid>`. A push to
  `main` builds and deploys in about 90 s. The `cf` CLI works only with the user sign-in:
  `env -u CLOUDFLARE_API_TOKEN cf …`.
- **Validation that loads each bundle per pipeline takes hours.** validate.py loads each page's bundles once; keep it
  that way when adding checks.

## What the code encodes

These rules were reverse-engineered from the published data; the verify scripts prove them on the base:

- `page_key` = first 16 hex characters of `sha256(test_id)`.
- Leaderboard rank: Overall descending, ties in CSV order.
- p90 latency uses the `nearest` percentile method; cost totals and per-page means use `math.fsum`; `page_std` uses ddof 1.
- Pairwise wins and losses use a tie tolerance of 1e-9.
- Sort orders: `page_dimension_scores` by (dimension, pipeline, score, test_id); parser rows by (score ascending,
  points_lost descending, test_id).
- The Layout dimension's label is "Layout (visual grounding)"; `layout__no_predictions` comes from the metric's
  `no_predictions` metadata.
- Dimension scores are the scorer's own `next_score`.
- Severity: `text_next` and `chart_next` events carry none, so it comes from the manifest's `severity_maps`. Chart
  `legend lost` maps to null, which is expected.
- Slot scores are `math.fsum / n`. Before the 2026-11-24 re-score about 205 of 826 published ones differed from every
  formula in the last binary digit; that re-score recomputed them all, so every pipeline now uses the formula.

## Files

| File | Does |
| --- | --- |
| `detect_changes.py` | step 1: what changed upstream, the verdict, and `pipelines.json` for it (read-only otherwise) |
| `score_local.sh` | score pipelines on this machine, as `.github/workflows/metrics.yml` does |
| `compare_scores.py` | deliverables' six dimensions against `leaderboard-v2.csv` (also the workflow's `check` job) |
| `fetch_base.py`, `fetch_outputs.py` | the dataset at a commit; pipelines' published outputs at their pins. Both reuse files on disk |
| `rows.py` | per-pipeline rows: scores, events, layout, bundle entries |
| `aggregates.py`, `index_files.py` | pipelines table, slots, page-dimension scores; `index/*.json` |
| `check_control.py`, `verify_aggregates.py`, `verify_index.py` | A5: prove the code on existing data |
| `build.py` | adds pipelines (`--add`) or rebuilds ones in the dataset (`--replace`) |
| `groundtruth.py` | ground-truth table, bundles and document links at a ParseBench-2 revision |
| `build_passes.sh` | full re-run build: every pipeline, in passes, over hard-linked trees |
| `full_rerun.sh` | F3: the whole full re-run from deliverables to a validated tree |
| `check_replace.py` | proof that `--replace` with a pipeline's own deliverable reproduces the base |
| `validate.py`, `finalize.py` | checks; manifest, validation report and README |
| `finalize_cfg.example.json` | the last full re-run's `finalize.py` settings, to copy |
| `changeset.py`, `upload.py`, `verify_upload.py`, `sync_main.py` | upload and its checks |
| `site_check/` | headless page check of the explorer, local build and production |

## History

- **2026-11-23**, 59 → 60 parsers: added `liteparse_ocria_nano`. Dataset `8fc443f` on branch `update-59-parsers`,
  then `main` synced at `9a042c2`. Results `e7cd5e1`, leaderboard `c8f7798`. Control GPT-6.2 sol (medium): PASS. All
  checks passed; 9,250 files changed. The single-commit upload answered 504 and had landed.
- **2026-11-24**, 60 → 61 parsers: added `ocria_1` (OCRia-1, rank 7) with this runbook. Scored locally
  (`score_local.sh`, register patch at `03fbc17`); control exact. Base assembled by `fetch_base.py` from the previous
  update's files (no downloads). Dataset `14a653c` on branch `update-61-parsers` (single commit, 4.5 min, no 504),
  `main` synced at `b6b3c67`. Results `497e243`, leaderboard `d6209da`. All checks passed; 9,251 files changed.
- **2026-11-24/25**, full re-score of all 61 parsers: scorer `d6209da`, ground truth `2e95407` (v2.08), leaderboard
  `d0c77ef` (upstream's re-score, before Hy4 Flash was added; Hy4 is held back while its results are on an open PR).
  Patches rebased (registration patch emptied). Scored on GitHub Actions in 34 min (run 45504854002); the control
  matched a local re-score on every page. Built in 7 passes. Fixes found on the way: `processing_duration_ms` is at
  a record's top level for some pipelines (rows.py now reads it there); validate's join check treated the 45
  no-output rows as mismatches. Dataset `aceefd8` on branch `rescore-2026-11-24` (504, landed, 8 min); 13,569 files
  changed; every file on the branch verified by hash.
- **2026-11-25**, no data change. Upstream added Tencent Hy4 Flash on an open results PR (`refs/pr/21`): held back.
  Turned this run's one-off fixes into code: two paths (add only, or full re-run) chosen by `detect_changes.py`,
  which now holds back unmerged pins and notes unregistered pipelines; `full_rerun.sh` (with `fetch_outputs.py`,
  `build_passes.sh`); `changeset.py --local`; `verify_upload.py`; finalize replaces files instead of writing through
  hard links and fills the Overall-rounding counts; the workflow checks scores against the leaderboard; the page check
  moved into `site_check/`. Local work data deleted except `work/seed` (44 GB freed). Proof of the full re-run path: `full_rerun.sh` (with `FORCE=1`) on the live dataset
  `797e049` with the deliverables of run 45504854002 rebuilt all 61 pipelines in 7 passes (25 min), validated, and,
  after `finalize.py`, matched the live dataset in every one of its 13,574 files (`changeset.py --local`: 0 changed).
