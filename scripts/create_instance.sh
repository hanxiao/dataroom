#!/bin/bash
# Create a GCP L4 GPU instance for DaaS (reproducible).
# Edit or pass via env: GCP_PROJECT, ZONE, NAME.
set -e

GCP_PROJECT="${GCP_PROJECT:-jinaai-dev}"
ZONE="${ZONE:-us-central1-a}"
NAME="${NAME:-daas-l4}"

gcloud compute instances create "$NAME" \
  --project="$GCP_PROJECT" \
  --zone="$ZONE" \
  --machine-type=g2-standard-8 \
  --image=pytorch-2-7-cu128-ubuntu-2204-nvidia-570-v20260129 \
  --image-project=deeplearning-platform-release \
  --boot-disk-size=150GB \
  --boot-disk-type=pd-ssd \
  --maintenance-policy=TERMINATE \
  --provisioning-model=SPOT \
  --instance-termination-action=STOP \
  --tags=daas

# Firewall: API (8000) + optional llama (8080). Idempotent.
gcloud compute firewall-rules create allow-daas-api \
  --project="$GCP_PROJECT" --allow=tcp:8000 --target-tags=daas 2>/dev/null || true
gcloud compute firewall-rules create allow-daas-llama \
  --project="$GCP_PROJECT" --allow=tcp:8080 --target-tags=daas 2>/dev/null || true

echo "Created $NAME in $ZONE. SSH then run scripts/setup.sh:"
echo "  gcloud compute ssh $NAME --project=$GCP_PROJECT --zone=$ZONE"
