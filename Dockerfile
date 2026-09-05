# SpreadsheetBench pipeline container.
#
#   docker build -t team .
#   docker run --rm -e TINKER_API_KEY=... -e TINKER_PROJECT_ID=... -v <dataset dir>:/data:ro -v <empty dir>:/out team
#   (OPENROUTER_API_KEY instead when --model names an OpenRouter model)
#
# Reads /data (dataset.json, spreadsheet/<id>/*init*.xlsx, prompt.txt), writes
# predictions.jsonl, outputs/, traces/, run.log to /out. Model-written code executes
# only inside this container. LibreOffice is included so the pipeline can recalculate
# formulas exactly the way the evaluator does.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONUNBUFFERED=1 \
    LC_ALL=C.UTF-8 \
    UV_LINK_MODE=copy

# LibreOffice headless for formula recalculation (same engine the grader uses)
RUN apt-get update \
    && apt-get install -y --no-install-recommends libreoffice-calc fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# dependencies first, for layer caching
COPY research/pyproject.toml research/uv.lock ./
# --extra tinker: the default backend (--model tinker) needs the tinker + tinker-cookbook optional dependencies
RUN uv sync --frozen --no-dev --extra tinker

# code
COPY research/sb.py sb.py
COPY research/baseline/ baseline/
COPY agent/ agent/

ENV PATH="/app/.venv/bin:$PATH"

# defaults inside run.py: --dataset-dir /data --out-dir /out
# extra flags can be appended: docker run ... team --ids 13-1,51-12 --mode null
ENTRYPOINT ["python", "agent/run.py"]
