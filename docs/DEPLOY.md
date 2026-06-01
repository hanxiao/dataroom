# Reproducible Deploy - Dataroom on a single L4

Everything is pinned and scripted. Two containers: `llama-server` (GPU) and `daas` (GPU app).

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
# set ONLY the Jina key (the only value you must set); replace jina_your_real_key
sed -i 's/^JINA_API_KEY=.*/JINA_API_KEY=jina_your_real_key/' .env
```

## 3. One-shot setup (Docker + NVIDIA toolkit + model + up)
```bash
bash scripts/setup.sh
```
This downloads `Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` (~22GB) into `models/`, builds the `daas`
image (which pre-bakes `jina-embeddings-v5-text-nano`), and starts both containers.

## Switching the model
With nothing set, the default is byte-for-byte today's Qwen3.6 serving. The model is unified
behind five env vars (set in `.env`); defaults shown reproduce today exactly.

| Env var | Default | Role |
| --- | --- | --- |
| `MODEL_FILE` | `Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf` | GGUF filename, shared: what `setup.sh` downloads into `models/` AND what `docker-compose`'s `llama-server --model` serves. A switch keeps download and serve in sync. |
| `MODEL_REPO` | `unsloth/Qwen3.6-35B-A3B-MTP-GGUF` | Hugging Face repo `setup.sh` pulls `MODEL_FILE` from. |
| `MODEL_ID` | `qwen3.6` | Agent label written to Pi's `models.json` (model id) and `settings.json` (`defaultModel`). llama.cpp's OpenAI endpoint accepts any id, so this only has to be internally consistent; it never needs to match the GGUF. |
| `CHAT_TEMPLATE_FILE` | `/templates/chat_template.jinja` | Path inside the llama-server container passed via `--chat-template-file`. |
| `SPEC_ARGS` | `--spec-type draft-mtp --spec-draft-n-max 2` | MTP / speculative-draft flags appended to the `llama-server` command, kept as one opaque string so the whole draft block drops or replaces atomically. |

Non-Qwen GGUF is not a pure filename swap:
- Chat template: the bundled `/templates/chat_template.jinja` is Qwen3.6-specific. Point
  `CHAT_TEMPLATE_FILE` at the new model's Jinja template, or remove the `--chat-template-file`
  flag from `docker-compose.yml` to use the GGUF's embedded template (compose cannot conditionally
  omit a flag, so an empty `CHAT_TEMPLATE_FILE` still passes an empty arg - edit the command
  instead). A wrong template silently corrupts tool-calling and reasoning.
- Draft / MTP: `--spec-type draft-mtp` requires a GGUF that ships an MTP draft head (the
  `...-MTP-GGUF` repo does). For a plain GGUF set `SPEC_ARGS=` (empty) to disable drafting.
- Context: `CTX_SIZE` / `CONTEXT_WINDOW` default 131072 is tuned to Qwen3.6's hybrid GDN+MoE KV
  math (see below). A dense model of similar size uses far more KV per token - lower `CTX_SIZE`
  accordingly or it may OOM on the L4.
- VRAM: the ~22GB / Q4_K_XL headroom analysis is Qwen-specific. Re-measure with `nvidia-smi`.

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
- Q4_K_XL weights ~22GB nearly fill the card. Qwen3.6-35B-A3B is **hybrid GDN+MoE**: only **10 of
  40 layers carry a per-token KV cache** (`full_attention_interval=4`); the other 30 are Gated
  DeltaNet layers with a small fixed recurrent state. So per-token KV ≈ `ctx * 10 layers * 2 (K+V)
  * 256 head_dim * 0.5 byte (q4_0)` ≈ `ctx * 5.12KB`: **~0.08GB at 16384, ~0.65GB at the full
  131072 window**. This is far smaller than a dense 35B's KV - do not size by dense rules; with Q4
  the weights, not the KV, are what fill VRAM.
- The `v5-nano` embedder runs on **CPU** by default (`EMBED_DEVICE=cpu`) so it does not compete
  for VRAM - with Q4 weights ~22GB nearly filling the L4, keeping the ~0.5GB embedder off-GPU
  avoids OOM (set `EMBED_DEVICE=cuda` to put it back on GPU if you have headroom). `-ngl` is left
  unset (auto-fit) with mmap on so light expert layers can spill to CPU. **Confirm with `nvidia-smi`.**
- If it gets tight, set `EMBED_DEVICE=cpu` (zero VRAM contention), lower `CTX_SIZE`, or use a
  smaller quant. Keep `CTX_SIZE` / `CONTEXT_WINDOW` / the dashboard denominator in sync (the
  default is 131072 everywhere).

## Hybrid prompt-cache caveat (correctness)
`--cache-reuse` is intentionally **disabled** in `docker-compose.yml`. This Gated-DeltaNet model
has documented recurrent-state cache drift (llama.cpp#21681) that can silently corrupt digits
across the long `--continue` + auto-compaction loop - fatal for a factual dataroom. Before
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
  - `models.json` -> default model = local Qwen (`http://llama-server:8080/v1`), id `MODEL_ID`
    (default `qwen3.6`); the same id is written as `settings.json` `defaultModel`
  - (no `mcp.json`) → Jina access is the `jina` CLI on PATH, called from bash; reads JINA_API_KEY from env
- It then loops `pi --mode json --continue` (the same per-cwd session resumes across process
  invocations) loading the `dataroom` skill and the `dataroom_index` extension. Qwen drives its
  own research loop; the orchestrator only enforces the floor/ceiling and zips `dataroom/`.

## Updating Pi / pinning llama.cpp
Pi is pinned via `PI_VERSION` in the Dockerfile (bump + `docker compose build daas`). Pin the
llama.cpp image too: set `LLAMA_IMAGE` in `.env` to a digest from
`docker buildx imagetools inspect ghcr.io/ggml-org/llama.cpp:server-cuda`.
