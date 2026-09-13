#!/usr/bin/env bash
# Aggiorna entrambi i repo e riavvia i container (da lanciare sul server).
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
PWA="${PWA_DIR:-../spec2026app}"
if [ -d "$PWA/.git" ]; then (cd "$PWA" && git pull --ff-only); fi
docker compose up -d --build
docker compose ps
