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
        curl ca-certificates git gnupg bc \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Pi coding agent (pinned for reproducibility)
ARG PI_VERSION=0.78.0
RUN npm install -g @earendil-works/pi-coding-agent@${PI_VERSION} && npm cache clean --force
ENV PI_BIN=pi PI_SKIP_VERSION_CHECK=1 PI_OFFLINE=0

WORKDIR /app

# Python deps. torch is already in the base image, so it is NOT reinstalled here.
COPY server/requirements.txt server/requirements.txt
RUN pip install -r server/requirements.txt

# Jina access is CLI-only: jina-cli (pure-python httpx+click) on PATH for the agent's bash
# tool. It covers search/read/rerank/embed/dedup/... and composes via pipes
# (`jina search Q | jina rerank R`, `cat urls.txt | jina read`) so bulky intermediates stay
# out of the LLM context. We deliberately do NOT install pi-mcp-adapter / wire Jina MCP: the
# model overwhelmingly drives via the shell, and CLI-only is leaner (no proxy tool, no adapter
# process, no run-to-run CLI-vs-MCP wobble). jina-grep-cli is Apple-Silicon/MLX-only and not
# installable here — the CUDA index_service.py is its analogue. jina reads JINA_API_KEY from env.
RUN pip install jina-cli

# App code
COPY server ./server
COPY pi ./pi
COPY templates ./templates
COPY web ./web
COPY assets ./assets

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
