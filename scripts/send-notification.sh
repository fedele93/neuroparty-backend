#!/usr/bin/env bash
# Invia una notifica push a tutti gli invitati dalla riga di comando.
# Uso:  ./scripts/send-notification.sh "🚌 Partenza navetta" "Partiamo tra 15 minuti dal Piazzale" Navetta
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
TITLE="${1:?titolo}"; BODY="${2:?messaggio}"; CAT="${3:-Organizzazione}"
curl -fsS -X POST "${PUBLIC_URL:-https://$DOMAIN}/api/notifications" \
  -H "Content-Type: application/json" -H "X-Admin-Token: $ADMIN_TOKEN" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"title":sys.argv[1],"message":sys.argv[2],"category":sys.argv[3]}))' "$TITLE" "$BODY" "$CAT")"
echo
