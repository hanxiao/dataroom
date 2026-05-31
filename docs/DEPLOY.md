# Reproducible Deploy — Dataroom-as-a-Service on a single L4

Everything is pinned and scripted. Two containers: `llama-server` (GPU) and `daas` (CPU app).

## 0. Prereqs
- GCP project with L4 quota (`g2-standard-8`), or any box with an NVIDIA L4 24GB + driver.
- A Jina API key: https://jina.ai/api-dashboard/

## 1. Create the GPU instance
```bash
GCP_PROJECT=jinaai-dev ZONE=us-central1-a NAME=daas-l4 bash scripts/create_instance.sh
gcloud compute ssh daas-l4 --project=jinaai-dev --zone=us-central1-a
```

## 2. Clone + configure
```bash
git clone https://github.com/hanxiao/dataroom-as-a-service.git
cd dataroom-as-a-service
cp .env.example .env
sed -i "s/jina_xxxx/$YOUR_JINA_KEY/" .env
```

## 3. One-shot setup (Docker + NVIDIA toolkit + model + up)
```bash
bash scripts/setup.sh
```
This downloads `Qwen3.6-35B-A3B-UD-Q3_K_XL.gguf` (~17GB) into `models/`, builds the `daas`
image (which pre-bakes `jina-embeddings-v5-text-nano`), and starts both containers.

## 4. Use it
```bash
# submit
JOB=$(curl -s -X POST localhost:8000/jobs -H 'content-type: application/json' \
  -d '{"query":"Competitive landscape of self-hosted small embedding models in 2026"}' \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')

# watch
watch -n 20 "curl -s localhost:8000/jobs/$JOB; echo; curl -s localhost:8000/jobs/$JOB/log | tail -5"

# download the zip when status=done
curl -s -OJ localhost:8000/jobs/$JOB/result
```

## VRAM / OOM notes (L4 24GB)
- Q3_K_XL weights ~17GB. Qwen3.6-35B-A3B is **hybrid GDN+MoE**: only **10 of 40 layers carry a
  per-token KV cache** (`full_attention_interval=4`); the other 30 are Gated DeltaNet layers
  with a small fixed recurrent state. So per-token KV ≈ `ctx * 10 layers * 2 (K+V) * 256 head_dim
  * 1 byte (q8_0)` ≈ `ctx * 10.24KB`: **~0.17GB at 16384, ~1.3GB at the full 131072 window**
  (≈2x those for f16 KV). This is far smaller than a dense 35B's KV — do not size by dense rules.
- The `v5-nano` embedder runs on the GPU by default (`EMBED_DEVICE=cuda`, ~0.5GB), sharing the
  L4. With weights ~17GB + KV ~1.3GB + embedder ~0.5GB you have comfortable headroom at 131072;
  the real pressure is the GDN recurrent-state pool + compute buffers. **Always confirm with
  `nvidia-smi`** rather than trusting any single number here.
- If it gets tight, set `EMBED_DEVICE=cpu` (zero VRAM contention), lower `CTX_SIZE`, or use a
  smaller quant. Keep `CTX_SIZE` / `CONTEXT_WINDOW` / the dashboard denominator in sync (the
  default is 131072 everywhere).

## Hybrid prompt-cache caveat (correctness)
`--cache-reuse` is intentionally **disabled** in `docker-compose.yml`. This Gated-DeltaNet model
has documented recurrent-state cache drift (llama.cpp#21681) that can silently corrupt digits
across the long `--continue` + auto-compaction loop — fatal for a factual dataroom. Before
re-enabling it on a pinned image, run a smoke test: feed a few known numeric facts through a
multi-turn compacting loop and diff the agent's recall against a fresh-prefill answer.

## Stopping & budget
Stopping is outcome-first (see `.env.example`): the loop runs until the **coverage floor** is met
(`MIN_FILES` substantive sourced files + all sub-questions closed + `SUMMARY.md`), or it saturates,
or a hard safety ceiling trips (`MAX_SECONDS` / `MAX_TURNS` / `MAX_JINA_CALLS`). A premature `DONE`
is rejected and the agent is nudged to keep going. The orchestrator writes `run_meta.json`
(`stop_reason`, floor metrics) and the dashboard shows the stop reason + progress-to-floor.

## How the autonomy works
- Per job, the orchestrator writes an isolated Pi agent dir (`PI_CODING_AGENT_DIR`) with:
  - `models.json` → default model = local Qwen (`http://llama-server:8080/v1`)
  - `mcp.json` → Jina MCP (`https://mcp.jina.ai/v1`)
- It then loops `pi --mode json --continue` (the same per-cwd session resumes across process
  invocations) loading the `dataroom` skill and the `dataroom_index` extension. Qwen drives its
  own research loop; the orchestrator only enforces the floor/ceiling and zips `dataroom/`.

## Updating Pi / pinning llama.cpp
Pi is pinned via `PI_VERSION` in the Dockerfile (bump + `docker compose build daas`). Pin the
llama.cpp image too: set `LLAMA_IMAGE` in `.env` to a digest from
`docker buildx imagetools inspect ghcr.io/ggml-org/llama.cpp:server-cuda`.
