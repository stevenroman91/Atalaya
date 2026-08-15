#!/bin/sh
# Arranque del servicio web: migraciones → admin inicial (idempotente) → uvicorn.
set -e
atalaya init-db
if [ -n "$ATALAYA_ADMIN_EMAIL" ] && [ -n "$ATALAYA_ADMIN_PASSWORD" ]; then
  atalaya create-admin || true
fi
exec atalaya serve --host 0.0.0.0 --port "${PORT:-8000}"
