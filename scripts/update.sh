#!/usr/bin/env bash
# Aggiorna entrambi i repo e riavvia i container (da lanciare sul server).
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
# PWA_DIR punta ai file statici (sottocartella, es. ../spec2026app/pwa), ma il repo git
# è alla radice (es. ../spec2026app). `--show-toplevel` trova la root da qualunque sottocartella.
PWA="${PWA_DIR:-../spec2026app/pwa}"
if PWA_ROOT="$(git -C "$PWA" rev-parse --show-toplevel 2>/dev/null)"; then
  (cd "$PWA_ROOT" && git pull --ff-only)
fi
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml ps
