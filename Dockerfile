# Atalaya — imagen única para el servicio web y los cron jobs de Railway.
FROM python:3.12-slim

# Dependencias de sistema: WeasyPrint (PDF) y lxml/trafilatura
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf-2.0-0 libcairo2 \
    libffi-dev shared-mime-info fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml alembic.ini ./
COPY src ./src
COPY migrations ./migrations
COPY config ./config
COPY locales ./locales
COPY scripts ./scripts

RUN pip install --no-cache-dir . && chmod +x scripts/*.sh

ENV ATALAYA_CONFIG_DIR=/app/config \
    ATALAYA_LOCALES_DIR=/app/locales \
    PYTHONUNBUFFERED=1

# El servicio web es el comando por defecto; los servicios cron de Railway
# lo sobreescriben con su startCommand (ver railway/*.json y README).
CMD ["./scripts/start-web.sh"]
