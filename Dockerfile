# geodata: ghcr.io/yibbzz/geodata-sync. docs/kubernetes.md runs `geodata sync` as a CronJob.
FROM python:3.14-slim

# GDAL's command-line tools (ogr2ogr, ogrinfo, gdal_translate) do the reading and loading
RUN apt-get update \
    && apt-get install -y --no-install-recommends gdal-bin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY geodata_sync/ geodata_sync/
RUN pip install --no-cache-dir . && rm -rf /app

# The project files go here (docs/kubernetes/Dockerfile copies them in), the Secrets under /etc/geodata/secrets
WORKDIR /project
ENV GEODATA_SECRETS_DIR=/etc/geodata/secrets
USER 65534
ENTRYPOINT ["geodata"]
CMD ["sync", "-C", "/project"]
