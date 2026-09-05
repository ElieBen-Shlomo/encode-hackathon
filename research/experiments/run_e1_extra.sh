#!/usr/bin/env bash
# Extra stage, queued behind run_e1.sh: xhigh thinking with the model's 32k completion cap, so the
# study can report what xhigh is worth when it is allowed to finish (at 8192 it truncates ~2/3 of tasks).
#   tmux new -d -s e1x 'bash experiments/run_e1_extra.sh'
set -uo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$PATH"
set -a; . ./.env; set +a
CONC="${CONC:-6}"
exec >> private/run_e1.log 2>&1
echo "== extra stage queued $(date -u +%FT%TZ); waiting for run_e1.sh to finish"
while pgrep -f "run_e1[.]sh" >/dev/null; do sleep 60; done
echo "== 7. extra: xhigh with max_tokens 32768 (values/tsv and agent/grid) $(date -u +%FT%TZ)"
FILTER='UserWarning|warn\(msg\)|unauthenticated|Task was destroyed|task: <Task'
uv run experiments/ablate_repr.py --concurrency "$CONC" --resume --skip-if-scored --project-id "$TINKER_PROJECT_ID" \
    --config mode=values,digest=tsv,reasoning=xhigh,max_tokens=32768 \
    --config mode=agent,digest=grid,reasoning=xhigh,max_tokens=32768 2>&1 | grep -v -E "$FILTER"
echo "== extra stage done $(date -u +%FT%TZ)"
