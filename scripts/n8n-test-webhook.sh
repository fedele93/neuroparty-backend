#!/usr/bin/env bash
# Collauda il collegamento backend -> n8n: invia un evento "test" all'URL in N8N_WEBHOOK_URL
# e stampa l'esito (delivered, codice HTTP, tentativi, eventuale errore).
# Uso:  ./scripts/n8n-test-webhook.sh            (usa PUBLIC_URL e ADMIN_TOKEN dal .env)
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
BASE="${PUBLIC_URL:-https://$DOMAIN}"
echo "Stato webhook:"
curl -sS "$BASE/api/automation/status" -H "X-Admin-Token: $ADMIN_TOKEN" \
  | python3 -c 'import json,sys; w=json.load(sys.stdin)["webhook"]; print(json.dumps({k: w[k] for k in ("enabled","url","secretConfigured","events","stats")}, indent=2, ensure_ascii=False))'
echo "Invio evento di prova..."
curl -sS -X POST "$BASE/api/automation/webhook-test" -H "X-Admin-Token: $ADMIN_TOKEN"
echo
