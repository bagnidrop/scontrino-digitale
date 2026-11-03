#!/bin/sh
# Copia di sicurezza del database. Esempio cron (ogni notte alle 3):
#   0 3 * * * cd /opt/scontrino && ./backup.sh
set -e
mkdir -p backups
docker compose exec -T app python - <<'PY' > "backups/scontrini-$(date +%F).db"
import sqlite3, sys
src = sqlite3.connect("/app/instance/scontrini.db")
dst = sqlite3.connect("/tmp/b.db"); src.backup(dst); dst.close()
sys.stdout.buffer.write(open("/tmp/b.db", "rb").read())
PY
find backups -name 'scontrini-*.db' -mtime +14 -delete
echo "Backup salvato in backups/"
