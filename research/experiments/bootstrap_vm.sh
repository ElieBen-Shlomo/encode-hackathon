#!/usr/bin/env bash
# One-shot setup of a fresh Debian 12 VM (e.g. GCP e2-standard-4) to run the E1 study unattended.
# Expects the repo working tree to have been copied to ~/encode-hackathon (gcloud compute scp), or
# clones BRANCH from REPO when that directory is absent.
#
#   bash bootstrap_vm.sh
#   # then: ~/encode-hackathon/research/.env must hold TINKER_API_KEY and TINKER_PROJECT_ID, and
#   cd ~/encode-hackathon/research && tmux new -d -s e1 'bash experiments/run_e1.sh'
set -euo pipefail
REPO="${REPO:-https://github.com/ElieBen-Shlomo/encode-hackathon.git}"
BRANCH="${BRANCH:-main}"

sudo apt-get update -y
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y git curl tmux rsync libreoffice-calc fonts-liberation fonts-dejavu-core
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
grep -q '.local/bin' ~/.bashrc || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

cd "$HOME"
if [ ! -d encode-hackathon ]; then
  git clone "$REPO"
  cd encode-hackathon && git checkout "$BRANCH" && cd ..
fi
cd encode-hackathon/research
uv sync --extra tinker
uv run data/download.py
uv run evaluate.py --oracle --quiet | tail -3
[ -f .env ] || printf 'TINKER_API_KEY=\nTINKER_PROJECT_ID=\n' > .env
soffice --headless --version
echo
echo "Setup done. Ensure $PWD/.env has TINKER_API_KEY and TINKER_PROJECT_ID, then:"
echo "  cd $PWD && tmux new -d -s e1 'bash experiments/run_e1.sh' && tail -f private/run_e1.log"
