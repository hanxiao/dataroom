# DaaS app container: Pi harness (Node) + FastAPI orchestrator + v5-nano embedder.
#
# Base: official PyTorch CUDA *runtime* image (torch + CUDA + cuDNN prebuilt, cu128 to
# match the GCP Deep Learning VM host). Using a prebuilt base avoids recompiling/reinstalling
# torch + the CUDA stack on every build. The embedder runs on the GPU by default (v5-nano is
# tiny, <1GB VRAM); set EMBED_DEVICE=cpu to fall back. The LLM runs in the separate
# llama-server (ghcr.io/ggml-org/llama.cpp:server-cuda) container, also prebuilt.
FROM pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime

ENV DEBIAN_FRONTEND=noninteractive PIP_NO_CACHE_DIR=1

# Node 22 (for Pi) + git. torch/CUDA already in the base, so no heavy compile here.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates git gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Pi coding agent (pinned for reproducibility)
ARG PI_VERSION=0.78.0
RUN npm install -g @earendil-works/pi-coding-agent@${PI_VERSION} && npm cache clean --force
ENV PI_BIN=pi PI_SKIP_VERSION_CHECK=1 PI_OFFLINE=0

# pi has no built-in MCP client; pi-mcp-adapter (pi's official MCP extension) bridges MCP
# servers (Jina) as a single `mcp` proxy tool — one ~200-token tool with lazy connect, not 20
# eager tool defs. Install it globally + runtime deps; the orchestrator loads it with
# --extension. It reads mcp.json from PI_CODING_AGENT_DIR (set per job).
ARG MCP_ADAPTER_VERSION=2.8.0
ENV PI_MCP_ADAPTER=/opt/pi-mcp/node_modules/pi-mcp-adapter/index.ts
RUN mkdir -p /opt/pi-mcp && cd /opt/pi-mcp \
    && npm init -y >/dev/null 2>&1 \
    && npm install pi-mcp-adapter@${MCP_ADAPTER_VERSION} && npm cache clean --force

WORKDIR /app

# Python deps. torch is already in the base image, so it is NOT reinstalled here.
COPY server/requirements.txt server/requirements.txt
RUN pip install -r server/requirements.txt

# jina-cli (pure-python httpx+click, OS-independent) on PATH for the agent's bash tool, so it
# can compose/pipe Jina ops (`jina search Q | jina rerank R`, `cat urls.txt | jina read`) and
# keep bulky intermediates out of the LLM context. Complements (does not replace) the `mcp`
# proxy, which stays primary for single search/read calls. NB: jina-grep-cli is Apple-Silicon
# /MLX-only and intentionally NOT installed here — the CUDA index_service.py is its analogue.
RUN pip install jina-cli

# App code
COPY server ./server
COPY pi ./pi
COPY templates ./templates
COPY web ./web

# Pre-bake the v5-nano weights into the image (CPU download at build time; no GPU needed
# during build) so the first job starts fast and the deploy is reproducible.
ENV HF_HOME=/app/.hf
RUN python -c "import os; os.environ['CUDA_VISIBLE_DEVICES']=''; \
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('jinaai/jina-embeddings-v5-text-nano', device='cpu', trust_remote_code=True)" \
    || echo "WARN: embed model prefetch skipped (will download on first run)"

ENV JOBS_DIR=/data/jobs
# Embedder device: cuda (default, shares the L4) or cpu (zero VRAM contention).
ENV EMBED_DEVICE=cuda
# Dashboard context bar denominator; keep in sync with llama-server --ctx-size.
ENV CONTEXT_WINDOW=131072
EXPOSE 8000
CMD ["python", "-m", "server.app"]
