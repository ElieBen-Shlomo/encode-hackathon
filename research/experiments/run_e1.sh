#!/usr/bin/env bash
# Unattended representation study (E1). Runs the whole ladder in priority order, logs everything,
# and is safe to re-run: finished configs are skipped, half-finished ones resume.
#
#   cd research && tmux new -d -s e1 'bash experiments/run_e1.sh'     # detached
#   tail -f research/private/run_e1.log                                # watch
#
# Needs research/.env with TINKER_API_KEY and TINKER_PROJECT_ID, LibreOffice installed, data downloaded.
# CONC = concurrent Tinker samples per process (default 6); two processes run per stage.
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"   # uv lives here; tmux shells are non-interactive and skip .bashrc
command -v uv >/dev/null || { echo "uv not found on PATH"; exit 1; }
set -a; . ./.env; set +a
: "${TINKER_API_KEY:?set TINKER_API_KEY in research/.env}"
: "${TINKER_PROJECT_ID:?set TINKER_PROJECT_ID in research/.env}"
CONC="${CONC:-6}"
mkdir -p private/runs
exec > >(tee -a private/run_e1.log) 2>&1
echo "== E1 start $(date -u +%FT%TZ) host=$(hostname) conc=$CONC project=$TINKER_PROJECT_ID"
DEV=$(paste -sd, eval/splits/dev100.txt)
FILTER='UserWarning|warn\(msg\)|unauthenticated|Task was destroyed|task: <Task'

run() {  # run a list of configs sequentially in one process
  uv run experiments/ablate_repr.py --concurrency "$CONC" --resume --skip-if-scored --project-id "$TINKER_PROJECT_ID" "$@" 2>&1 | grep -v -E "$FILTER"
}
both() {  # run two config lists in parallel, wait for both
  local n=$(( $# / 2 ))
  run "${@:1:$n}" &
  run "${@:$(( n + 1 ))}" &
  wait
}

echo "== 1. E0: shipped baseline through Tinker (values, tsv, xhigh, 8192 tokens)"
if [ ! -f private/runs/E0-tinker_predict/results.json ]; then
  uv run baseline/tinker_predict.py --out-dir private/runs/E0-tinker_predict --base-model Qwen/Qwen3.8-27B \
      --project-id "$TINKER_PROJECT_ID" --ids "$DEV" --concurrency "$CONC" --max-tokens 8192 2>&1 | grep -v -E "$FILTER"
  uv run evaluate.py --predictions private/runs/E0-tinker_predict/predictions.jsonl --ids "$DEV" \
      --out private/runs/E0-tinker_predict/results.json --quiet 2>&1 | grep -v -E "$FILTER"
  uv run experiments/attribute.py private/runs/E0-tinker_predict 2>&1 | head -2
fi

echo "== 1b. same config through the harness (must reproduce E0 within noise)"
run --config mode=values,digest=tsv,reasoning=xhigh,max_tokens=8192

echo "== 2. thinking sweep on the baseline view"
both --config mode=values,digest=tsv,reasoning=off --config mode=values,digest=tsv,reasoning=low \
     --config mode=values,digest=tsv,reasoning=medium --config mode=values,digest=tsv,reasoning=adaptive
run  --config mode=values,digest=tsv,reasoning=xhigh

echo "== 2b. reading probes on every view (low thinking)"
uv run eval/probes.py --reasoning low --project-id "$TINKER_PROJECT_ID" --concurrency "$CONC" \
    --digest tsv --digest windowed --digest grid --digest compact --digest markdown --digest html \
    --digest json --digest addressed --digest schema 2>&1 | grep -v -E "$FILTER" || echo "probes failed (continuing)"

echo "== 3. factorial: content {tsv,grid} x thinking {low,medium} x solver {values,agent}, plus windowed"
both --config mode=values,digest=grid,reasoning=low  --config mode=values,digest=grid,reasoning=medium \
     --config mode=agent,digest=tsv,reasoning=low    --config mode=agent,digest=tsv,reasoning=medium
both --config mode=agent,digest=grid,reasoning=low   --config mode=agent,digest=grid,reasoning=medium \
     --config mode=values,digest=windowed,reasoning=low --config mode=agent,digest=windowed,reasoning=low

echo "== 4. layouts under both solvers at low thinking (winner unknown when unattended, so both cells)"
both --config mode=values,digest=compact,reasoning=low --config mode=values,digest=markdown,reasoning=low \
     --config mode=values,digest=html,reasoning=low    --config mode=values,digest=json,reasoning=low
both --config mode=values,digest=addressed,reasoning=low --config mode=agent,digest=schema,reasoning=low \
     --config mode=agent,digest=compact,reasoning=low    --config mode=agent,digest=markdown,reasoning=low

echo "== 5. budget curve on grid (values, low) plus one agent point"
both --config mode=values,digest=grid,reasoning=low,budget=2000 --config mode=values,digest=grid,reasoning=low,budget=4000 \
     --config mode=values,digest=grid,reasoning=low,budget=16000 --config mode=agent,digest=grid,reasoning=low,budget=4000

echo "== 6. paired comparisons against the baseline cell (values-tsv-low)"
for d in private/runs/values-*-low* private/runs/agent-*-low*; do
  if [ -f "$d/results.json" ] && [ "$d" != private/runs/values-tsv-low ]; then
    echo "--- values-tsv-low vs $(basename "$d")"
    uv run experiments/compare.py private/runs/values-tsv-low "$d" 2>&1 | grep -v -E "$FILTER" | head -4
  fi
done
echo "== E1 done $(date -u +%FT%TZ)"
