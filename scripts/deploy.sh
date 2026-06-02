#!/usr/bin/env bash
# Fast local redeploy of the daas app on the Docker host - much faster than waiting for the
# GitHub Actions image build. With a warm BuildKit cache the heavy layers (torch base, apt,
# pi, pip deps, the v5-nano weight prefetch) are all reused, so only the app-code COPY layers
# rebuild: seconds, not minutes.
#
# Run from the repo root ON THE BOX after the code is in place (git pull / scp / git-archive
# extract). The GitHub workflow still builds + publishes the canonical ghcr image on push; this
# is just the quick inner-loop path.
#
#   ./scripts/deploy.sh            # build (warm cache) + recreate the daas container
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose build daas
docker compose up -d daas
docker compose ps daas
