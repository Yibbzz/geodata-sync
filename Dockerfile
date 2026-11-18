# dataops: ghcr.io/yibbzz/geoserver-dataops. docs/kubernetes.md runs `dataops sync` as a CronJob.
FROM python:3.14-slim

# GDAL's command-line tools (ogr2ogr, ogrinfo, gdal_translate) do the reading and loading
RUN apt-get update \
    && apt-get install -y --no-install-recommends gdal-bin \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY geoserver_dataops/ geoserver_dataops/
RUN pip install --no-cache-dir '.[overture,sftp]' && rm -rf /app

# The project files go here (docs/kubernetes/Dockerfile copies them in), the Secrets under /etc/dataops/secrets
WORKDIR /project
ENV DATAOPS_SECRETS_DIR=/etc/dataops/secrets
USER 65534
ENTRYPOINT ["dataops"]
CMD ["sync", "-C", "/project"]
