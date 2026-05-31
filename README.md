# Dataroom-as-a-Service (DaaS)

Give it a research query. It spins up an autonomous [Pi](https://pi.dev) harness that keeps
researching the web until the dataroom is **comprehensive** (a measurable coverage floor — not
a fixed time/turn cap), incrementally building a well-organized **dataroom** on disk.

- **Brain**: [Pi coding agent](https://pi.dev) (`@earendil-works/pi-coding-agent`) running headless.
- **Default LLM**: self-hosted **Qwen3.6-35B-A3B** (the same model + MTP serving used by
  [`ki-extractor`](https://github.com/hanxiao/ki-extractor)) on a single **L4 24GB** GPU via llama.cpp.
- **Tools exposed to the agent** (no giant prompt — just expose tools and let it drive):
  - **Jina MCP** (`search_web`, `read_url`, `embeddings`, ...) via `https://mcp.jina.ai/v1`,
    bridged by pi-mcp-adapter as one lazy `mcp` proxy tool; plus **`jina-cli`** on PATH for
    composable/piped ops (`jina search Q | jina rerank R`) that keep intermediates out of context
  - **jina-embeddings-v5-nano** preloaded for the dataroom index (embed / semantic search / dedup),
    with server-side reconciliation so it never silently drifts from disk
  - `read` / `write` / `edit` / `bash` (Pi built-ins) — so it can also write code, verify, and plot
- **Output**: when it stops, an async job returns a **`.zip` of the whole dataroom**.
- **Live dashboard** (turbopuffer-style black/white): real-time context utilization, throughput,
  tool-call distribution, **live activity feed**, **warnings/errors**, **progress-to-floor**, a
  **stop-reason banner**, and the dataroom file tree. At `GET /jobs/{id}/dashboard`.

## Design philosophy

The agent is *not* micromanaged. We expose Jina MCP + an embedding-backed dataroom index
and a one-page methodology skill, then let Qwen drive its own loop. Before adding anything
to the dataroom it must `dataroom_index search` first to avoid duplicates and keep structure.

```
HTTP POST /jobs {query}                  async job (uuid)
   -> orchestrator (run_dataroom.py)
        loop:
          pi --mode json --continue  (same per-cwd session)   <- autonomous Qwen turns
          agent uses Jina MCP + jina-cli + dataroom_index + bash
          until the coverage FLOOR is met (DONE is rejected before then),
          or it saturates (diminishing returns), or a hard safety ceiling trips
   -> zip dataroom/  ->  GET /jobs/{id}/result  (download .zip)
```

Stopping is **outcome-first**: `DONE` is only honored once the dataroom holds enough
substantive sourced files (`MIN_FILES`, default 100), all sub-questions are closed, and a
`SUMMARY.md` exists. Turns/seconds/Jina-calls are only hard backstops. The reason it stopped
is surfaced on the dashboard. See [`docs/DEPLOY.md`](docs/DEPLOY.md) and `.env.example`.

Both containers use **prebuilt** base images (`pytorch/pytorch:*-runtime` for the app,
`ghcr.io/ggml-org/llama.cpp:server-cuda` for the LLM, pinnable via `LLAMA_IMAGE`) so there is
no torch/CUDA recompile.

## Architecture (containers)

```
llama-server (:8080)   GPU   Qwen3.6-35B-A3B UD-Q3_K_XL + MTP draft, ctx 131072 (q8_0 KV)
daas        (:8000)    GPU   FastAPI + Pi harness + v5-nano embedding (~0.5GB)
```

VRAM budget on L4 (24GB): Q3_K_XL weights ~17GB. Qwen3.6-35B-A3B is a **hybrid GDN+MoE**
model — only **10 of 40 layers carry a per-token KV cache** (the other 30 are Gated DeltaNet
layers keeping a small fixed recurrent state), so per-token KV is tiny: roughly `ctx*10.24KB`
at q8_0, i.e. **~1.3GB at the full 131072 window** (not the multi-GB a dense 35B would need).
Default `CTX_SIZE=131072` targets Qwen3.6's native window; the real headroom pressure is the
GDN recurrent-state pool + compute buffers + the embedder, so **verify with `nvidia-smi` and
lower `CTX_SIZE` only if it's tight.** The v5-nano embedder (~239M params, ~0.5GB) runs on the
GPU by default (`EMBED_DEVICE=cuda`); set `EMBED_DEVICE=cpu` for zero VRAM contention.

The Pi context window, the dashboard's context-utilization bar, and Pi's built-in
auto-compaction all key off the same `CTX_SIZE`, so they stay consistent automatically.

## Reproducible deploy (single L4 GPU)

```bash
# 1. Create the GPU box (GCP L4, spot)
bash scripts/create_instance.sh           # edit GCP_PROJECT/ZONE at top, or pass as env

# 2. On the box: one-shot setup (Docker + NVIDIA toolkit + model download + up)
git clone https://github.com/hanxiao/dataroom-as-a-service.git
cd dataroom-as-a-service
cp .env.example .env && $EDITOR .env      # set JINA_API_KEY
bash scripts/setup.sh

# 3. Submit a job
curl -s -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"query":"Competitive landscape of self-hosted small embedding models in 2026"}'
# -> {"job_id":"...."}

# 4. Watch the live dashboard (or poll), then download the zip
open http://<host>:8000/jobs/<job_id>/dashboard
curl -s localhost:8000/jobs/<job_id>          # raw status
curl -s -OJ localhost:8000/jobs/<job_id>/result
```

There is also a minimal submit page at `GET /` and a JSON stats feed at `GET /jobs/{id}/stats`.

See [`docs/DEPLOY.md`](docs/DEPLOY.md) for the full reproducible walkthrough.

## Local dev (no GPU)

Point the agent at any OpenAI-compatible endpoint (or Qwen demo box) via `LLAMA_URL`,
then run the harness directly:

```bash
uv venv && uv pip install -r server/requirements.txt
JINA_API_KEY=... LLAMA_URL=http://<host>:8080 \
  python -m server.run_dataroom --query "your query" --out ./out
```

## License

MIT
