# DaaS app container: Pi harness (Node) + FastAPI orchestrator + v5-nano embedder.
# The embedder runs on the GPU by default (v5-nano is tiny, <1GB VRAM) for speed,
# sharing the L4 with the LLM. Set EMBED_DEVICE=cpu to fall back to CPU.
# The LLM itself runs in the separate llama-server (GPU) container.
FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# Node 22 + Python + build basics
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git python3 python3-pip python3-venv gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Pi coding agent (pinned for reproducibility)
ARG PI_VERSION=0.78.0
RUN npm install -g @earendil-works/pi-coding-agent@${PI_VERSION} \
    && npm cache clean --force
ENV PI_BIN=pi
ENV PI_SKIP_VERSION_CHECK=1
ENV PI_OFFLINE=0

WORKDIR /app

# Python deps. CUDA torch (cu124 wheels) so the embedder can use the GPU.
# The cuda base ships pip 22.0.2 (no --break-system-packages); upgrade pip first.
COPY server/requirements.txt server/requirements.txt
RUN python3 -m pip install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir \
        -r server/requirements.txt \
        --extra-index-url https://download.pytorch.org/whl/cu124

# App code
COPY server ./server
COPY pi ./pi
COPY templates ./templates
COPY web ./web

# Pre-bake the v5-nano weights into the image (download on CPU at build time; no GPU
# during build) so the first job starts fast and the deploy is reproducible.
ENV HF_HOME=/app/.hf
RUN python3 -c "import os; os.environ['CUDA_VISIBLE_DEVICES']=''; \
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('jinaai/jina-embeddings-v5-text-nano', device='cpu', trust_remote_code=True)" \
    || echo "WARN: embed model prefetch skipped (will download on first run)"

ENV JOBS_DIR=/data/jobs
# Embedder device: cuda (default, shares the L4) or cpu (zero VRAM contention).
ENV EMBED_DEVICE=cuda
# Dashboard context bar denominator; keep in sync with llama-server --ctx-size
ENV CONTEXT_WINDOW=16384
EXPOSE 8000
CMD ["python3", "-m", "server.app"]
