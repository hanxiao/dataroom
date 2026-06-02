# Run on Apple Silicon (Mac, Metal)

Dataroom runs natively on an Apple Silicon Mac with **no Docker and no NVIDIA GPU** — the
`llama-server` from Homebrew's `llama.cpp` serves the model on **Metal**, and the FastAPI app +
Pi agent + embedder run in a local `uv` virtualenv. No application code changes are needed; the
model is decoupled behind the OpenAI-compatible `LLAMA_URL`, so the only Mac-specific concerns are
which GGUF to use, the `llama-server` flags, and installing the deps the Docker image normally
bundles.

Tested on an **M3 Pro / 36 GB**, macOS, `llama.cpp` build 8890 (Homebrew). At least **32 GB** of
unified memory is recommended — the Q4 model wires ~22 GB.

## What's different from the NVIDIA/Docker path

| Area | NVIDIA / Docker | Apple Silicon | Why |
| --- | --- | --- | --- |
| Model GGUF | `unsloth/Qwen3.6-35B-A3B-MTP-GGUF` | `unsloth/Qwen3.6-35B-A3B-GGUF` (**non-MTP**) | The MTP variant ships an extra speculative-draft head (`blk.40`); the Homebrew build loads it as a normal layer and fails with `missing tensor 'blk.40.ssm_conv1d.weight'`. |
| Speculative decode | `--spec-type draft-mtp --spec-draft-n-max 2` | dropped (`SPEC_ARGS=` empty) | Current Homebrew `llama.cpp` `--spec-type` only has `ngram-*`, no `draft-mtp`. Decode is somewhat slower but correct. Opt back in by exporting `SPEC_ARGS=...` once a Metal build supports it. |
| GPU offload | `-ngl` auto-fit (spill experts to CPU to avoid L4 OOM) | `-ngl 999` (`NGL` env) | Unified memory: put all layers on Metal. |
| Flash attention | `--flash-attn 1` | `--flash-attn on` | This build's flag takes `on\|off\|auto`, not `1` (needed for the `q4_0` KV cache). |
| Serving | `llama.cpp:server-cuda` container | Homebrew `llama-server` (Metal) | No CUDA / `nvidia-container-toolkit` on macOS. |
| App + embedder | Docker container | `python -m server.app` in a `uv` venv; embedder on CPU | No GPU passthrough into Docker on macOS; CPU keeps Metal free for the LLM. |
| torch | from the CUDA base image | `uv pip install torch` (MPS/CPU build) | Not pinned in `server/requirements.txt`. |

## Prerequisites

```bash
brew install llama.cpp          # Metal build of llama-server
# Node 22 (Pi agent) + uv (Python env), e.g. via mise:
#   mise use -g node@22
#   curl -LsSf https://astral.sh/uv/install.sh | sh
node --version    # v22.x
uv --version
```

You also need a free **Jina API key** (https://jina.ai/api-dashboard/) and, recommended, a free
**Hugging Face read token** (https://huggingface.co/settings/tokens) for a fast, stable model
download.

## 1. Install the agent + Python deps

```bash
npm install -g @earendil-works/pi-coding-agent@0.78.0

# torch is NOT in server/requirements.txt (upstream got it from a CUDA base image),
# so install it explicitly — it pulls the Apple-Silicon (MPS/CPU) build.
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python \
  torch -r server/requirements.txt jina-cli huggingface-hub
```

## 2. Download the model (~22 GB, non-MTP GGUF)

```bash
mkdir -p models
HF_TOKEN=hf_your_token \
.venv/bin/python -c "from huggingface_hub import hf_hub_download; \
hf_hub_download('unsloth/Qwen3.6-35B-A3B-GGUF','Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf',local_dir='models')"
```

The `HF_TOKEN=` prefix is optional but avoids the unauthenticated rate limit (which can stall the
download). `hf_hub_download` caches and resumes if interrupted. A smaller **Q3_K_XL** (~17 GB)
from the same repo frees more memory if you are constrained.

## 3. Set your key

```bash
cp .env.example .env
sed -i '' 's/^JINA_API_KEY=.*/JINA_API_KEY=jina_your_real_key/' .env
```

Recommended Mac `.env` values (defaults baked into `scripts/mac-run.sh`, override as needed):

```bash
MODEL_FILE=Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf
CTX_SIZE=65536            # comfortable inside 36 GB; raise toward 131072 if you have headroom
CONTEXT_WINDOW=65536
LLAMA_URL=http://127.0.0.1:8080
JOBS_DIR=./data/jobs
EMBED_DEVICE=cpu          # leave Metal's memory for the LLM
```

## 4. Run

```bash
bash scripts/mac-run.sh
```

Starts `llama-server` (Metal) on `:8080`, waits for it to load (~30 s), then starts the app on
`:8000`. Open **http://localhost:8000/**, or watch a job at `http://localhost:8000/jobs/{id}/dashboard`.

Stop the app with `Ctrl+C`; stop the model with `pkill -f llama-server`.

## Memory notes

On a 36 GB machine the model wires ~22-25 GB; the system sits near full memory use with the
compressor active. It runs without OOM, but if a long job pages heavily, lower `CTX_SIZE`
(e.g. `32768`) in `.env`. The v5-nano embedder stays on CPU (`EMBED_DEVICE=cpu`) precisely so
Metal's memory is reserved for the LLM.

## Headless (no web UI)

```bash
JINA_API_KEY=... LLAMA_URL=http://127.0.0.1:8080 \
  .venv/bin/python -m server.run_dataroom --query "your query" --out ./out
```

## Switching the model

Set `MODEL_FILE` in `.env` to a different GGUF in `models/` and restart. The bundled
`templates/chat_template.jinja` is **Qwen3.6-specific**; for a non-Qwen GGUF set
`CHAT_TEMPLATE_FILE` to that model's own Jinja template (a wrong template silently corrupts
tool-calling).
</content>
