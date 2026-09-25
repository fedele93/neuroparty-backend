#!/usr/bin/env bash
# Programma il backup notturno (ore 03:15) nel crontab dell'utente corrente. Idempotente.
# Uso:  ./scripts/install-backup-cron.sh        Rimozione:  ./scripts/install-backup-cron.sh --remove
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LINE="15 3 * * * cd $REPO && ./scripts/backup.sh >> $REPO/backups/backup.log 2>&1"
MARK="# neuroparty-backup"

current="$(crontab -l 2>/dev/null || true)"
filtered="$(printf '%s\n' "$current" | grep -v "$MARK" || true)"
if [ "${1:-}" = "--remove" ]; then
  printf '%s\n' "$filtered" | crontab -
  echo "Backup notturno rimosso dal crontab."
  exit 0
fi
mkdir -p "$REPO/backups"
{ printf '%s\n' "$filtered"; echo "$LINE $MARK"; } | sed '/^$/d' | crontab -
echo "Backup notturno programmato alle 03:15: $LINE"
echo "Verifica con: crontab -l"
