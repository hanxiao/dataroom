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
llama-server (:8080)   GPU   Qwen3.6-35B-A3B UD-Q3_K_XL + MTP draft, ctx 131072 (q8_0 KV)
daas        (:8000)    GPU   FastAPI + Pi harness + v5-nano embedding (~0.5GB)
```

VRAM budget on L4 (24GB): Q3_K_XL weights ~17GB leave ~6GB for the KV cache. With
`--flash-attn` + **q8_0 KV quantization** the context window stretches far past the Q4 setups
(ki-extractor's Q4 weights are ~22GB so it can only do 8K-16K; our Q3 frees room for much
more). Default `CTX_SIZE=131072` targets Qwen3.6's full native window — **verify with
`nvidia-smi` on your box and lower `CTX_SIZE` (e.g. 65536) if it OOMs.** The v5-nano embedder
(~212M params, ~0.5GB) runs on the GPU by default (`EMBED_DEVICE=cuda`); set `EMBED_DEVICE=cpu`
to move it off-GPU if you need the headroom.

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
