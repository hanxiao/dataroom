#!/bin/bash
# One-shot reproducible setup for Dataroom-as-a-Service on a GCP L4 GPU instance.
# Usage: bash scripts/setup.sh
set -e
cd "$(dirname "$0")/.."

MODEL_REPO="${MODEL_REPO:-unsloth/Qwen3.6-35B-A3B-MTP-GGUF}"
MODEL_FILE="${MODEL_FILE:-Qwen3.6-35B-A3B-UD-Q3_K_XL.gguf}"

if [ ! -f .env ]; then
  echo "Create .env from .env.example and set JINA_API_KEY first." >&2
  exit 1
fi

echo "=== Installing Docker ==="
if ! command -v docker &>/dev/null; then
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER" || true
fi

echo "=== Installing NVIDIA Container Toolkit ==="
if ! dpkg -l | grep -q nvidia-container-toolkit; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
    sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
  sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
fi

echo "=== Downloading model ($MODEL_FILE) ==="
mkdir -p models data
if [ ! -f "models/$MODEL_FILE" ]; then
  pip install -q huggingface-hub || pip3 install -q --break-system-packages huggingface-hub
  python3 -c "from huggingface_hub import hf_hub_download; \
hf_hub_download('$MODEL_REPO', '$MODEL_FILE', local_dir='models')"
else
  echo "Model already present."
fi

echo "=== Building + starting services ==="
sudo docker compose up -d --build

echo "=== Waiting for llama-server ==="
for i in $(seq 1 90); do
  if curl -fsS http://localhost:8080/health >/dev/null 2>&1; then echo "llama-server ready"; break; fi
  echo "waiting llama... ($i/90)"; sleep 5
done

echo "=== Waiting for DaaS API ==="
for i in $(seq 1 30); do
  if curl -fsS http://localhost:8000/health 2>/dev/null | grep -q '"ok":true'; then echo "DaaS API ready"; break; fi
  echo "waiting api... ($i/30)"; sleep 3
done

IP=$(curl -s ifconfig.me || echo localhost)
echo ""
echo "=== Deployment complete ==="
echo "DaaS API:         http://$IP:8000"
echo "llama-server API: http://$IP:8080"
echo ""
echo "Submit a job:"
echo "  curl -s -X POST http://$IP:8000/jobs -H 'content-type: application/json' -d '{\"query\":\"...\"}'"
