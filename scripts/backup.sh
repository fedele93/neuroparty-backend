#!/usr/bin/env bash
# Backup della cartella data/ (DB SQLite, foto, chiavi VAPID) in backups/, con rotazione.
# Uso:  ./scripts/backup.sh            (da lanciare sul server, anche da cron)
# Variabili: BACKUP_DIR (default ./backups), KEEP_DAYS (default 7)
set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP_DAYS="${KEEP_DAYS:-7}"
STAMP="$(date +%F_%H%M)"
mkdir -p "$BACKUP_DIR"

# Copia consistente del database mentre l'API è in funzione (API di backup online di SQLite).
# Se il container non è attivo, copia il file così com'è (SQLite in modalità WAL resta leggibile).
if docker compose ps --status running api >/dev/null 2>&1 && docker compose ps --status running api 2>/dev/null | grep -q api; then
  docker compose exec -T api python -c "import sqlite3; s=sqlite3.connect('/data/neuroparty.db'); d=sqlite3.connect('/data/neuroparty.backup.db'); s.backup(d); d.close(); s.close()" \
    || echo "Avviso: copia online del DB fallita, uso il file così com'è" >&2
fi

tar czf "$BACKUP_DIR/backup-neuroparty-$STAMP.tgz" data/
rm -f data/neuroparty.backup.db
echo "Backup creato: $BACKUP_DIR/backup-neuroparty-$STAMP.tgz ($(du -h "$BACKUP_DIR/backup-neuroparty-$STAMP.tgz" | cut -f1))"

# Rotazione: elimina i backup più vecchi di KEEP_DAYS giorni
find "$BACKUP_DIR" -name 'backup-neuroparty-*.tgz' -mtime +"$KEEP_DAYS" -print -delete | sed 's/^/Rimosso vecchio backup: /'
