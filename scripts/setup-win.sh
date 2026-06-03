#!/bin/bash
# setup-win.sh
# Dataroom setup for Windows + WSL2 + Docker Desktop

set -e
cd "$(dirname "$0")/.."

MODEL_DEFAULT="unsloth/Qwen3.6-35B-A3B-MTP-GGUF/Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf"

# ---------------------------------------------------------------------
# JINA API KEY VALIDATION
# ---------------------------------------------------------------------

ENV_KEY="${JINA_API_KEY:-}"

if [ ! -f .env ]; then
  if [ -n "$ENV_KEY" ] && [ "$ENV_KEY" != "jina_xxxx" ]; then
    cp .env.example .env
    sed -i "s|^JINA_API_KEY=.*|JINA_API_KEY=$ENV_KEY|" .env
    echo "Created .env using supplied JINA key."
  else
    echo "ERROR: JINA_API_KEY not configured."
    echo "Either:"
    echo "  cp .env.example .env"
    echo "  edit JINA_API_KEY"
    echo ""
    echo "or"
    echo ""
    echo "  JINA_API_KEY=jina_xxx bash scripts/setup-win.sh"
    exit 1
  fi
fi

JINA_API_KEY="$(grep -E '^JINA_API_KEY=' .env | head -n1 | cut -d= -f2-)"

if [ -z "$JINA_API_KEY" ] || [ "$JINA_API_KEY" = "jina_xxxx" ]; then
  echo "ERROR: Invalid JINA_API_KEY."
  exit 1
fi

# ---------------------------------------------------------------------
# MODEL RESOLUTION
# ---------------------------------------------------------------------

MODEL="${MODEL:-$(grep -E '^MODEL=' .env | head -n1 | cut -d= -f2-)}"
MODEL="${MODEL:-$MODEL_DEFAULT}"

MODEL_REPO="${MODEL%/*}"
MODEL_FILE="${MODEL##*/}"

if grep -q '^MODEL_FILE=' .env; then
  sed -i "s|^MODEL_FILE=.*|MODEL_FILE=$MODEL_FILE|" .env
else
  echo "MODEL_FILE=$MODEL_FILE" >> .env
fi

# ---------------------------------------------------------------------
# DOCKER DESKTOP VALIDATION
# ---------------------------------------------------------------------

echo "=== Checking Docker Desktop ==="

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: Docker CLI not found inside WSL."
  echo ""
  echo "Install Docker Desktop and enable:"
  echo "  Settings -> Resources -> WSL Integration"
  exit 1
fi

docker version >/dev/null

if ! docker compose version >/dev/null 2>&1; then
  echo "ERROR: docker compose unavailable."
  exit 1
fi

echo "Docker Desktop detected."

# ---------------------------------------------------------------------
# GPU VALIDATION
# ---------------------------------------------------------------------

echo "=== Checking GPU support ==="

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi || true
else
  echo "WARNING: nvidia-smi not available in WSL."
fi

if docker run --rm --gpus all \
    nvidia/cuda:12.4.1-base-ubuntu22.04 \
    nvidia-smi >/dev/null 2>&1; then
  echo "Docker GPU support detected."
else
  echo ""
  echo "ERROR: Docker Desktop GPU support unavailable."
  echo ""
  echo "Verify:"
  echo "  1. Latest NVIDIA driver installed"
  echo "  2. WSL2 enabled"
  echo "  3. Docker Desktop WSL integration enabled"
  echo "  4. docker run --gpus all works"
  exit 1
fi

# ---------------------------------------------------------------------
# DISK CHECK
# ---------------------------------------------------------------------

AVAIL_GB="$(df -P . | awk 'NR==2{print int($4/1024/1024)}')"

if [ "$AVAIL_GB" -lt 40 ]; then
  echo "WARNING: Only ${AVAIL_GB}GB free."
  echo "Model + containers may require >30GB."
fi

# ---------------------------------------------------------------------
# MODEL DOWNLOAD
# ---------------------------------------------------------------------

echo "=== Downloading model ==="

mkdir -p models data

if [ ! -f "models/$MODEL_FILE" ]; then

  if command -v uv >/dev/null 2>&1; then

    uv run --with huggingface-hub python -c "
from huggingface_hub import hf_hub_download
hf_hub_download(
    '$MODEL_REPO',
    '$MODEL_FILE',
    local_dir='models'
)
"

  else

    python3 -m pip install -q huggingface-hub

    python3 -c "
from huggingface_hub import hf_hub_download
hf_hub_download(
    '$MODEL_REPO',
    '$MODEL_FILE',
    local_dir='models'
)
"
  fi

else
  echo "Model already present."
fi

# ---------------------------------------------------------------------
# START SERVICES
# ---------------------------------------------------------------------

echo "=== Starting containers ==="

if [ "${DAAS_PULL:-}" = "1" ]; then
  docker compose pull
  docker compose up -d
else
  docker compose up -d --build
fi

# ---------------------------------------------------------------------
# LLAMA HEALTH CHECK
# ---------------------------------------------------------------------

echo "=== Waiting for llama-server ==="

LLAMA_OK=0
for i in $(seq 1 90); do

  if curl -fsS http://localhost:8080/health >/dev/null 2>&1; then
    echo "llama-server ready"
    LLAMA_OK=1
    break
  fi

  echo "waiting llama ($i/90)"
  sleep 5
done

if [ "$LLAMA_OK" != 1 ]; then
  echo "ERROR: llama-server did not become ready (loads ~22GB; check: docker compose logs llama-server)" >&2
  exit 1
fi

# ---------------------------------------------------------------------
# API HEALTH CHECK
# ---------------------------------------------------------------------

echo "=== Waiting for DaaS API ==="

API_OK=0
for i in $(seq 1 30); do

  if curl -fsS http://localhost:8000/health 2>/dev/null \
      | grep -q '"ok":true'; then
    echo "API ready"
    API_OK=1
    break
  fi

  echo "waiting api ($i/30)"
  sleep 3
done

if [ "$API_OK" != 1 ]; then
  echo "ERROR: DaaS API did not become ready (check: docker compose logs daas)" >&2
  exit 1
fi

# ---------------------------------------------------------------------
# OUTPUT
# ---------------------------------------------------------------------

echo ""
echo "=== Deployment Complete ==="
echo ""
echo "DaaS API:"
echo "  http://localhost:8000"
echo ""
echo "llama-server:"
echo "  http://localhost:8080"
echo ""
echo "Test:"
echo "  curl -X POST http://localhost:8000/jobs \\"
echo "       -H 'content-type: application/json' \\"
echo "       -d '{\"query\":\"hello\"}'"
