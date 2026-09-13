#!/usr/bin/env bash
# Installazione su un server Ubuntu (22.04/24.04) "pulito": Docker + clone dei due repo.
# Uso:  curl -fsSL https://raw.githubusercontent.com/fedele93/neuroparty-backend/main/scripts/install-ubuntu.sh | bash
set -euo pipefail

BASE_DIR="${BASE_DIR:-$HOME/neuroparty}"

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Installo Docker (script ufficiale get.docker.com)"
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker "$USER" || true
fi

mkdir -p "$BASE_DIR" && cd "$BASE_DIR"
[ -d spec2026app ] || git clone https://github.com/fedele93/spec2026app.git
[ -d neuroparty-backend ] || git clone https://github.com/fedele93/neuroparty-backend.git

cd neuroparty-backend
if [ ! -f .env ]; then
  cp .env.example .env
  TOKEN=$(openssl rand -hex 24 2>/dev/null || head -c 48 /dev/urandom | base64 | tr -d '/+=' | head -c 48)
  sed -i "s|^ADMIN_TOKEN=.*|ADMIN_TOKEN=$TOKEN|" .env
  echo
  echo "==> Creato .env con un ADMIN_TOKEN casuale: $TOKEN"
  echo "    Ora modifica .env e imposta DOMAIN, ACME_EMAIL, PUBLIC_URL, VAPID_SUBJECT."
  echo "    Poi:  cd $BASE_DIR/neuroparty-backend && docker compose up -d --build"
else
  echo "==> .env già presente, non lo tocco."
fi
