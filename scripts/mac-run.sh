#!/bin/bash
# Run the Dataroom stack natively on an Apple Silicon Mac (Metal) - no Docker, no NVIDIA.
#
#   llama-server (Homebrew, Metal)   ->  :8080  (OpenAI-compatible)
#   FastAPI app  (.venv, server.app) ->  :8000  (web UI + dashboard + API)
#
# The Pi agent and the v5-nano embedder run inside the app process tree (embedder on CPU).
# See docs/MAC.md for the full setup. This script only LAUNCHES; install steps live there.
#
# BACKEND selects the :8080 server: 'llamacpp' (default, GGUF via llama.cpp) or 'mlx' (mlx-lm,
# Apple-native, ~6x faster prefill). mlx uses .venv-mlx + models/mlx/... and auto-caps context to
# MLX_CTX_CAP (fp16-KV OOM headroom). See docs/MAC.md.
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

# Inference backend: llamacpp (GGUF via llama.cpp, default) | mlx (mlx-lm, Apple-native).
BACKEND="${BACKEND:-llamacpp}"

[ -x "$ROOT/.venv/bin/python" ] || { echo "ERROR: .venv missing. See docs/MAC.md (uv venv + uv pip install)." >&2; exit 1; }
command -v pi >/dev/null || { echo "ERROR: pi not found. Install: npm install -g @earendil-works/pi-coding-agent@0.78.0" >&2; exit 1; }
case "$BACKEND" in
  llamacpp)
    command -v llama-server >/dev/null || { echo "ERROR: llama-server not found. Install: brew install llama.cpp" >&2; exit 1; } ;;
  mlx)
    [ -x "$ROOT/.venv-mlx/bin/mlx_lm.server" ] || { echo "ERROR: .venv-mlx is missing mlx-lm. Create it: uv venv .venv-mlx && VIRTUAL_ENV=\$PWD/.venv-mlx uv pip install mlx-lm  (see docs/MAC.md)" >&2; exit 1; } ;;
  *) echo "ERROR: BACKEND must be 'llamacpp' or 'mlx' (got '$BACKEND')" >&2; exit 1 ;;
esac

MODEL_FILE="${MODEL_FILE:-mtp/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf}"
CTX_SIZE="${CTX_SIZE:-65536}"
NGL="${NGL:-999}"
SPEC_ARGS="${SPEC_ARGS:---spec-type draft-mtp --spec-draft-n-max 2}"   # ~1.23x on Metal; set SPEC_ARGS= to disable
CHAT_TEMPLATE_FILE="${CHAT_TEMPLATE_FILE:-$ROOT/templates/chat_template.jinja}"
MODEL_PATH="$ROOT/models/$MODEL_FILE"

# --- MLX backend knobs (only used when BACKEND=mlx) ---
MLX_MODEL="${MLX_MODEL:-$ROOT/models/mlx/Qwen3.6-35B-A3B-UD-MLX-4bit}"
# KV cache quantization. Stock mlx_lm.server ran fp16 KV (OOM ~78-92K actual tokens on a 36GB
# Mac). The --kv-bits flag (ml-explore/mlx-lm#1353) pushes the ceiling to ~92-113K with 4-bit KV
# (greedy-lossless, long-ctx recall verified). We auto-detect the flag below: when present we
# default to 4-bit KV and the higher context cap; otherwise fp16 KV with the conservative cap.
MLX_KV_BITS="${MLX_KV_BITS:-4}"
MLX_KV_GROUP_SIZE="${MLX_KV_GROUP_SIZE:-64}"
MLX_KV_ARGS=()

if [ "$BACKEND" = "mlx" ]; then
  [ -d "$MLX_MODEL" ] || { echo "ERROR: MLX model not found: $MLX_MODEL  (convert with mlx_lm.convert; see docs/MAC.md)" >&2; exit 1; }
  # mlx_lm.server resolves the request's `model` field against the loaded model (and otherwise
  # tries to fetch it from HuggingFace). llama.cpp ignores the label, but MLX needs it to match
  # the loaded path, so pin Pi's MODEL_ID to it. (llama.cpp keeps the friendly default.)
  export MODEL_ID="${MODEL_ID:-$MLX_MODEL}"
  # Use quantized KV only if this mlx_lm.server build supports it; raise the cap accordingly.
  if [ -n "$MLX_KV_BITS" ] && "$ROOT/.venv-mlx/bin/mlx_lm.server" --help 2>/dev/null | grep -q -- '--kv-bits'; then
    MLX_KV_ARGS=(--kv-bits "$MLX_KV_BITS" --kv-group-size "$MLX_KV_GROUP_SIZE")
    MLX_CTX_CAP="${MLX_CTX_CAP:-85000}"
    echo "NOTE: BACKEND=mlx using ${MLX_KV_BITS}-bit KV (group $MLX_KV_GROUP_SIZE); ctx cap $MLX_CTX_CAP"
  else
    MLX_CTX_CAP="${MLX_CTX_CAP:-75000}"
    [ -n "$MLX_KV_BITS" ] && echo "NOTE: this mlx_lm.server lacks --kv-bits (need mlx-lm#1353); fp16 KV, ctx cap $MLX_CTX_CAP"
  fi
  if [ "$CTX_SIZE" -gt "$MLX_CTX_CAP" ]; then
    echo "NOTE: BACKEND=mlx caps CTX_SIZE $CTX_SIZE -> $MLX_CTX_CAP"
    CTX_SIZE="$MLX_CTX_CAP"
  fi
else
  [ -f "$MODEL_PATH" ] || { echo "ERROR: model not found: $MODEL_PATH  (see docs/MAC.md to download the GGUF)" >&2; exit 1; }
fi

mkdir -p logs "${JOBS_DIR:-./data/jobs}"

# Put the venv (jina CLI) and pi on PATH for the agent's bash tool.
export PATH="$ROOT/.venv/bin:$(dirname "$(command -v pi)"):$PATH"
export PI_BIN="$(command -v pi)"
export PI_SKIP_VERSION_CHECK=1

# Wait for the :8080 server to answer /health, or tail its log and bail.
wait_for_server() {
  local label="$1" logf="$2"
  echo -n "waiting for $label"
  for i in $(seq 1 120); do
    if curl -fsS "http://127.0.0.1:8080/health" >/dev/null 2>&1; then echo " ready"; return 0; fi
    echo -n "."; sleep 2
    [ "$i" = 120 ] && { echo " TIMEOUT"; tail -30 "$logf"; exit 1; }
  done
}

# --- 1. inference server (:8080) ----------------------------------------------
if curl -fsS "http://127.0.0.1:8080/health" >/dev/null 2>&1; then
  echo "inference server already up on :8080"
elif [ "$BACKEND" = "mlx" ]; then
  echo "=== starting mlx_lm.server (Metal) - loads ~19GB, first run ~30-60s ==="
  nohup "$ROOT/.venv-mlx/bin/mlx_lm.server" \
    --model "$MLX_MODEL" \
    --host 127.0.0.1 --port 8080 \
    --max-tokens 8192 \
    ${MLX_KV_ARGS[@]+"${MLX_KV_ARGS[@]}"} \
    > "$ROOT/logs/mlx.log" 2>&1 &
  echo "mlx_lm.server PID: $!  (logs: logs/mlx.log)"
  wait_for_server "mlx_lm.server" "$ROOT/logs/mlx.log"
else
  echo "=== starting llama-server (Metal) - loads ~22GB, first run ~30-60s ==="
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
  wait_for_server "llama-server" "$ROOT/logs/llama.log"
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
echo "    backend:    $BACKEND"
if [ "$BACKEND" = "mlx" ]; then
  echo "    LLAMA_URL:  $LLAMA_URL    ctx=$CTX_SIZE (cap $MLX_CTX_CAP)    model=$MLX_MODEL    embedder=$EMBED_DEVICE"
  if [ ${#MLX_KV_ARGS[@]} -gt 0 ]; then
    echo "    note:       ${MLX_KV_BITS}-bit KV (group $MLX_KV_GROUP_SIZE, mlx-lm#1353); dashboard tok/s + KV gauge are llama.cpp-only"
  else
    echo "    note:       fp16 KV (this mlx_lm.server lacks --kv-bits, see mlx-lm#1353); dashboard tok/s + KV gauge are llama.cpp-only"
  fi
else
  echo "    LLAMA_URL:  $LLAMA_URL    ctx=$CTX_SIZE    ngl=$NGL    embedder=$EMBED_DEVICE"
  [ -z "$SPEC_ARGS" ] && echo "    spec:       (disabled)" || echo "    spec:       $SPEC_ARGS"
fi
exec "$ROOT/.venv/bin/python" -m server.app
