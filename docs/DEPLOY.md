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
- Q3_K_XL weights ~17GB. KV cache at `--ctx-size 16384 --parallel 1 --flash-attn 1` ~4GB.
  Total ~21GB, leaving ~2-3GB headroom. Do **not** raise `--parallel` or `--ctx-size`
  without re-checking `nvidia-smi`.
- The embedder (`v5-nano`) and FastAPI run in the `daas` container with
  `CUDA_VISIBLE_DEVICES=` → CPU only → zero VRAM contention with the LLM.
- If you bump context for very large datarooms, drop `n-predict` or use a smaller quant.

## How the autonomy works
- Per job, the orchestrator writes an isolated Pi agent dir (`PI_CODING_AGENT_DIR`) with:
  - `models.json` → default model = local Qwen (`http://llama-server:8080/v1`)
  - `mcp.json` → Jina MCP (`https://mcp.jina.ai/v1`)
- It then loops `pi --mode json --continue` (same session) loading the `dataroom` skill and
  the `dataroom_index` extension. Qwen drives its own research loop; we only supervise budgets
  and stop when `STATUS.md` starts with `DONE`, then zip `dataroom/`.

## Updating Pi
Pinned via `PI_VERSION` build arg in the Dockerfile. Bump it and `docker compose build daas`.
