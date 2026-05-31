# DaaS app container: Pi harness (Node) + FastAPI orchestrator + v5-nano embedder (CPU).
# The LLM runs in the separate llama-server (GPU) container.
FROM node:22-bookworm-slim

# Python + build basics
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Pi coding agent (pinned for reproducibility) + TS extension runtime deps it needs
ARG PI_VERSION=0.78.0
RUN npm install -g @earendil-works/pi-coding-agent@${PI_VERSION} \
    && npm cache clean --force
ENV PI_BIN=pi
ENV PI_SKIP_VERSION_CHECK=1
ENV PI_OFFLINE=0

WORKDIR /app

# Python deps (CPU torch to keep the image lean; embedder is CPU-only)
COPY server/requirements.txt server/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages \
        -r server/requirements.txt \
        --extra-index-url https://download.pytorch.org/whl/cpu

# App code
COPY server ./server
COPY pi ./pi
COPY templates ./templates
COPY web ./web

# Pre-bake the v5-nano embedder into the image so first job is fast and deploy is reproducible
ENV CUDA_VISIBLE_DEVICES=""
ENV HF_HOME=/app/.hf
RUN python3 -c "import os; os.environ['CUDA_VISIBLE_DEVICES']=''; \
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('jinaai/jina-embeddings-v5-text-nano', device='cpu', trust_remote_code=True)" \
    || echo "WARN: embed model prefetch skipped (will download on first run)"

ENV JOBS_DIR=/data/jobs
# Dashboard context bar denominator; keep in sync with llama-server --ctx-size
ENV CONTEXT_WINDOW=16384
EXPOSE 8000
CMD ["python3", "-m", "server.app"]
