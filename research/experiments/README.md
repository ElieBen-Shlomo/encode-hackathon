# Representation study (E1): how Qwen should read the spreadsheet

Owner: Maanav. Everything runs from `research/` with `uv run`. Runs land in `research/private/runs/`
(gitignored); only the scoreboard tables in this folder are committed. Nothing here opens a golden file.

## Setup once

```sh
cd research
printf 'TINKER_API_KEY=<key>\nTINKER_PROJECT_ID=<project id from Elie>\n' >> .env   # .env is gitignored
uv run eval/make_splits.py            # eval/splits/{dev100,check50,rest250}.txt (already committed)
.venv/bin/tinker auth status          # credential valid?
.venv/bin/tinker billing usage        # credit burn, check after E0 and after the thinking sweep
```

## The configuration the study selected (now the defaults)

Code agent, `grid` view, `low` thinking, 32k completion cap with the step-down ladder, 400 tasks in flight,
local LibreOffice, sandbox and workbook-read work each bounded on its own thread pool (CPU count, 2x, 2x).
dev-100: 88% against 52% for the shipped setup
(see `representation_ablation.md`). Both entry points default to it:

```sh
uv run agent/run.py --dataset-dir <data> --out-dir <out>      # flags: --digest --reasoning --max-tokens --concurrency --lo-concurrency --sandbox-concurrency --reads-concurrency --retries --call-timeout
uv run baseline/agent_predict.py --out-dir <out>              # reads research/config/qwen.yaml (renderer, digest, concurrency, ...)
```

Before relying on 400 in flight, run dev-100 once at that setting and watch the `attempts` field in traces
(retries on throttling) and tasks per minute; if Tinker throttles, lower `--concurrency`.

## Views (`--digest`) and thinking levels (`--reasoning`)

| digest | what the model sees |
|---|---|
| `tsv` | shipped baseline: values only, every sheet, blind 120x30 cap |
| `windowed` | Lorcan's digest: values only, head rows + window around the answer |
| `grid` | formulas as `=F -> value` (recalculated init), typed column headers with number formats, dates marked, defined names, merged ranges, region-of-interest window under a token budget |
| `markdown` / `html` / `json` / `addressed` | same content as `grid`, different shape |
| `compact` | SpreadsheetLLM-style: structural anchors kept, homogeneous row runs collapsed, per-column aggregates |
| `schema` | for the code solver: column table + a few sample rows, size-invariant |

`--reasoning off|low|medium|xhigh|adaptive` picks the Qwen3.8 renderer (`adaptive`: low for 20 or fewer graded cells, else medium).
`--mode values` is the baseline strategy (JSON cell values); `--mode agent` is Lorcan's Python loop.

## Run order

```sh
DEV=$(paste -sd, eval/splits/dev100.txt)

# 1. E0: the shipped baseline through Tinker (values, tsv, xhigh, 8192 tokens). Expect ~59%.
uv run baseline/tinker_predict.py --out-dir private/runs/E0-tinker_predict --base-model Qwen/Qwen3.8-27B --ids "$DEV" --concurrency 8
uv run evaluate.py --predictions private/runs/E0-tinker_predict/predictions.jsonl --ids "$DEV" --out private/runs/E0-tinker_predict/results.json --quiet

# 1b. Same config through the new harness; must reproduce E0 within noise.
uv run experiments/ablate_repr.py --config mode=values,digest=tsv,reasoning=xhigh,max_tokens=8192

# 2. Thinking sweep on the baseline view (picks the two levels for the factorial; gives the latency curve).
uv run experiments/ablate_repr.py --config mode=values,digest=tsv,reasoning=off \
    --config mode=values,digest=tsv,reasoning=low --config mode=values,digest=tsv,reasoning=medium \
    --config mode=values,digest=tsv,reasoning=xhigh --config mode=values,digest=tsv,reasoning=adaptive

# 2b. Reading probes: can it read each view at all? Minutes per view.
uv run eval/probes.py --reasoning low --digest tsv --digest windowed --digest grid --digest compact \
    --digest markdown --digest html --digest json --digest addressed --digest schema

# 3. Stage 1 factorial: content {tsv, grid} x thinking {L1, L2} x solver {values, agent}, plus windowed.
uv run experiments/ablate_repr.py \
    --config mode=values,digest=grid,reasoning=low  --config mode=values,digest=grid,reasoning=medium \
    --config mode=agent,digest=tsv,reasoning=low    --config mode=agent,digest=tsv,reasoning=medium \
    --config mode=agent,digest=grid,reasoning=low   --config mode=agent,digest=grid,reasoning=medium \
    --config mode=values,digest=windowed,reasoning=low

# 4. Stage 2 layouts on the winning cell (example: values + low).
uv run experiments/ablate_repr.py --config mode=values,digest=compact,reasoning=low \
    --config mode=values,digest=markdown,reasoning=low --config mode=values,digest=html,reasoning=low \
    --config mode=values,digest=json,reasoning=low --config mode=values,digest=addressed,reasoning=low \
    --config mode=agent,digest=schema,reasoning=low

# 5. Budget curve on the winner.
uv run experiments/ablate_repr.py --config mode=values,digest=grid,reasoning=low,budget=2000 \
    --config mode=values,digest=grid,reasoning=low,budget=4000 --config mode=values,digest=grid,reasoning=low,budget=16000

# Compare any two runs, paired on the same tasks:
uv run experiments/compare.py private/runs/values-tsv-low private/runs/values-grid-low
# Failure buckets for one run:
uv run experiments/attribute.py private/runs/values-grid-low
```

Every run appends a row to `experiments/scoreboard.md`; probes append to `experiments/probes.md`.
Re-score without re-running: add `--score-only`. Resume a killed run: add `--resume`.

## Running unattended on the GCP VM

VM `e1-runner` (Debian 12, e2-standard-4, zone `europe-west2-b`, project `priv-mkt-hack26lon-3700`). It holds a
copy of this working tree at `~/encode-hackathon` (not a git remote: copy changed files over with
`gcloud compute scp`). `experiments/bootstrap_vm.sh` sets a fresh VM up; `experiments/run_e1.sh` is the
unattended ladder (priority order, resume on interruption, scoring, attribution, paired comparisons).

```sh
# start (detached; survives closing the laptop)
gcloud compute ssh e1-runner --zone europe-west2-b --command 'cd ~/encode-hackathon/research && tmux new -d -s e1 "bash experiments/run_e1.sh"'

# watch
gcloud compute ssh e1-runner --zone europe-west2-b --command 'tail -n 40 ~/encode-hackathon/research/private/run_e1.log'
gcloud compute ssh e1-runner --zone europe-west2-b --command 'cat ~/encode-hackathon/research/experiments/scoreboard.md'

# stop everything
gcloud compute ssh e1-runner --zone europe-west2-b --command 'tmux kill-session -t e1; pkill -f agent/run.py; pkill -f ablate_repr'

# pull results back (runs are gitignored, so copy them)
gcloud compute scp --recurse e1-runner:~/encode-hackathon/research/private/runs ./private/ --zone europe-west2-b
gcloud compute scp e1-runner:~/encode-hackathon/research/experiments/scoreboard.md e1-runner:~/encode-hackathon/research/experiments/probes.md ./experiments/ --zone europe-west2-b

# when finished with the VM (billing stops)
gcloud compute instances stop e1-runner --zone europe-west2-b
```

## Tests (fast, no model, no credits)

```sh
cd research && uv run pytest tests -q        # ~90 s; LibreOffice-dependent tests skip if soffice is absent
```

`research/tests/` pins the grader's semantics, answer-position parsing on the awkward real cases, every view on
nine edge-case workbooks, the mock end-to-end pipeline (one predictions line per task, trace contract, resume),
the sandbox (input copy, timeout, key stripping), Tinker reply handling against a fake sampler (truncation never
parsed as an answer), the failure buckets and paired bootstrap, the probe grader, and golden hygiene (static scan
plus a runtime trap on every workbook open during a mock run). Run it before every PR.

## Reading the scoreboard

- `pass %` is the ranking metric; `cell acc %` the tie-break. With n=100, unpaired gaps under 8-10 points are noise; use `compare.py`.
- Buckets: `infra` (call failed / not graded), `truncation` (answer range outside the window), `format` (right value, wrong type), `coverage` (nothing written), `error_value` (#NAME? etc.), `reasoning` (rest). A view earns its place by shrinking truncation and format.
- `in tok p50/p90` is prompt size; `out tok mean` includes thinking tokens; `s/task` is wall time per task at the run's concurrency.
- `length hits` counts calls that hit `max_tokens` (thinking too long to finish).
