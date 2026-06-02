#!/bin/bash
# Run the Dataroom stack natively on an Apple Silicon Mac (Metal) — no Docker, no NVIDIA.
#
#   llama-server (Homebrew, Metal)   ->  :8080  (OpenAI-compatible)
#   FastAPI app  (.venv, server.app) ->  :8000  (web UI + dashboard + API)
#
# The Pi agent and the v5-nano embedder run inside the app process tree (embedder on CPU).
# See docs/MAC.md for the full setup. This script only LAUNCHES; install steps live there.
#
# It honours the same env knobs as the docker-compose path (MODEL_FILE, CTX_SIZE, SPEC_ARGS,
# CHAT_TEMPLATE_FILE), just with Mac-appropriate defaults:
#   - SPEC_ARGS defaults to '--spec-type draft-mtp --spec-draft-n-max 2' (measured ~1.23x
#     decode speedup on M3 Pro with llama.cpp >= 9430). Set SPEC_ARGS= to disable if your
#     build lacks draft-mtp support.
#   - --flash-attn on   (the CUDA compose passes `1`; this build wants on|off|auto).
#   - NGL defaults to 999 (unified memory: all layers on Metal; no L4 spill tradeoff).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

set -a; [ -f .env ] && . ./.env; set +a

JINA_API_KEY="${JINA_API_KEY:-}"
if [ -z "$JINA_API_KEY" ] || [ "$JINA_API_KEY" = "jina_xxxx" ]; then
  echo "ERROR: set a real JINA_API_KEY in .env (free key: https://jina.ai/api-dashboard/)" >&2
  exit 1
fi

command -v llama-server >/dev/null || { echo "ERROR: llama-server not found. Install: brew install llama.cpp" >&2; exit 1; }
[ -x "$ROOT/.venv/bin/python" ] || { echo "ERROR: .venv missing. See docs/MAC.md (uv venv + uv pip install)." >&2; exit 1; }
command -v pi >/dev/null || { echo "ERROR: pi not found. Install: npm install -g @earendil-works/pi-coding-agent@0.78.0" >&2; exit 1; }

MODEL_FILE="${MODEL_FILE:-mtp/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf}"
CTX_SIZE="${CTX_SIZE:-65536}"
NGL="${NGL:-999}"
SPEC_ARGS="${SPEC_ARGS:---spec-type draft-mtp --spec-draft-n-max 2}"   # ~1.23x on Metal; set SPEC_ARGS= to disable
CHAT_TEMPLATE_FILE="${CHAT_TEMPLATE_FILE:-$ROOT/templates/chat_template.jinja}"
MODEL_PATH="$ROOT/models/$MODEL_FILE"
[ -f "$MODEL_PATH" ] || { echo "ERROR: model not found: $MODEL_PATH  (see docs/MAC.md to download the GGUF)" >&2; exit 1; }

mkdir -p logs "${JOBS_DIR:-./data/jobs}"

# Put the venv (jina CLI) and pi on PATH for the agent's bash tool.
export PATH="$ROOT/.venv/bin:$(dirname "$(command -v pi)"):$PATH"
export PI_BIN="$(command -v pi)"
export PI_SKIP_VERSION_CHECK=1

# --- 1. llama-server (Metal) --------------------------------------------------
if curl -fsS "http://127.0.0.1:8080/health" >/dev/null 2>&1; then
  echo "llama-server already up on :8080"
else
  echo "=== starting llama-server (Metal) — loads ~22GB, first run ~30-60s ==="
  # shellcheck disable=SC2086
  nohup llama-server \
    --model "$MODEL_PATH" \
    --host 127.0.0.1 --port 8080 \
    --metrics \
    --ctx-size "$CTX_SIZE" \
    --parallel 1 \
    --flash-attn on \
    --cache-type-k q4_0 --cache-type-v q4_0 \
    -ngl "$NGL" \
    -ub 256 -b 2048 \
    --n-predict 8192 \
    --jinja \
    --chat-template-file "$CHAT_TEMPLATE_FILE" \
    $SPEC_ARGS \
    > "$ROOT/logs/llama.log" 2>&1 &
  echo "llama-server PID: $!  (logs: logs/llama.log)"
  echo -n "waiting for llama-server"
  for i in $(seq 1 120); do
    if curl -fsS "http://127.0.0.1:8080/health" >/dev/null 2>&1; then echo " ready"; break; fi
    echo -n "."; sleep 2
    [ "$i" = 120 ] && { echo " TIMEOUT"; tail -30 "$ROOT/logs/llama.log"; exit 1; }
  done
fi

# --- 2. FastAPI app -----------------------------------------------------------
export LLAMA_URL="${LLAMA_URL:-http://127.0.0.1:8080}"
export JOBS_DIR="${JOBS_DIR:-$ROOT/data/jobs}"
export CONTEXT_WINDOW="${CONTEXT_WINDOW:-$CTX_SIZE}"
export EMBED_DEVICE="${EMBED_DEVICE:-cpu}"
export TOKENIZERS_PARALLELISM="${TOKENIZERS_PARALLELISM:-false}"
export PORT="${PORT:-8000}"

echo "=== starting Dataroom app on :$PORT ==="
echo "    web UI:     http://localhost:$PORT/"
echo "    LLAMA_URL:  $LLAMA_URL    ctx=$CTX_SIZE    ngl=$NGL    embedder=$EMBED_DEVICE"
[ -z "$SPEC_ARGS" ] && echo "    spec:       (disabled)" || echo "    spec:       $SPEC_ARGS"
exec "$ROOT/.venv/bin/python" -m server.app
