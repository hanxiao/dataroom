# Dataroom-as-a-Service (DaaS)

Give it a research query. It spins up an autonomous [Pi](https://pi.dev) harness that
loops forever (until the dataroom is comprehensive or a budget is hit), crawling and
researching the web, and incrementally building a well-organized **dataroom** on disk.

- **Brain**: [Pi coding agent](https://pi.dev) (`@earendil-works/pi-coding-agent`) running headless.
- **Default LLM**: self-hosted **Qwen3.6-35B-A3B** (the same model + MTP serving used by
  [`ki-extractor`](https://github.com/hanxiao/ki-extractor)) on a single **L4 24GB** GPU via llama.cpp.
- **Tools exposed to the agent** (no giant prompt — just expose tools and let it drive):
  - **Jina MCP** (`search_web`, `read_url`, `embeddings`, ...) via `https://mcp.jina.ai/v1`
  - **jina-embeddings-v5-nano** preloaded on CPU for the dataroom index (embed / semantic search / dedup)
  - `read` / `write` / `edit` / `bash` (Pi built-ins) — so it can also write code, verify, and plot
- **Output**: when done, an async job returns a **`.zip` of the whole dataroom**.
- **Live dashboard** (turbopuffer-style black/white): real-time context utilization, tool-call
  count + distribution, dataroom file tree, and dataroom size. At `GET /jobs/{id}/dashboard`.

## Design philosophy

The agent is *not* micromanaged. We expose Jina MCP + an embedding-backed dataroom index
and a one-page methodology skill, then let Qwen drive its own loop. Before adding anything
to the dataroom it must `dataroom_index search` first to avoid duplicates and keep structure.

```
HTTP POST /jobs {query}                  async job (uuid)
   -> orchestrator (run_dataroom.py)
        loop:
          pi --mode json --continue  (same session)   <- autonomous Qwen turns
          agent uses Jina MCP + dataroom_index + bash
          until STATUS.md == DONE  or  budget exhausted
   -> zip dataroom/  ->  GET /jobs/{id}/result  (download .zip)
```

## Architecture (containers)

```
llama-server (:8080)   GPU   Qwen3.6-35B-A3B UD-Q3_K_XL + MTP draft, ctx 16384  (~17GB VRAM)
daas        (:8000)    CPU   FastAPI + Pi harness + v5-nano embedding (CUDA hidden)
```

VRAM budget on L4 (24GB): Q3_K_XL weights ~17GB + KV cache (ctx 16384, parallel 1, flash-attn)
~4GB, headroom kept ~2-3GB so it does not OOM. The embedding model runs **CPU-only**
(`CUDA_VISIBLE_DEVICES=`) exactly like ki-extractor, so it never competes for VRAM.

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
