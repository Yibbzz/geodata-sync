#!/usr/bin/env bash
# Start a GeoServer and PostGIS for the e2e tests (pytest -m e2e).
#   tests/e2e/up.sh            GeoServer Cloud: gateway, REST, WFS and WCS (about 2.5 GB of memory)
#   tests/e2e/up.sh standard   standard GeoServer
#   tests/e2e/up.sh standard-2 standard GeoServer 2.28, the older series still maintained
#   tests/e2e/up.sh down       stop and remove everything
# The versions are those GeoServer ConfigOps tests on (its tests/e2e/up.sh), the README's "Tested on".
set -euo pipefail
cd "$(dirname "$0")"
export GEOSERVER_TAG=3.0.2.1  # GeoServer Cloud
export POSTGIS_IMAGE=postgis/postgis:18-3.7
export RABBITMQ_IMAGE=rabbitmq:4.4-alpine
export STANDARD_GEOSERVER_VERSION=3.0.2  # the newest standard GeoServer
export STANDARD_2_GEOSERVER_VERSION=2.28.6  # the newest of the 2.28 series
if [ "${1:-}" = "down" ]; then
  docker compose --profile cloud --profile standard down -v
  exit 0
fi
PROFILE=cloud SERVICE=rest
[ "${1:-}" = "standard" ] && PROFILE=standard SERVICE=geoserver
if [ "${1:-}" = "standard-2" ]; then
  PROFILE=standard SERVICE=geoserver STANDARD_GEOSERVER_VERSION=$STANDARD_2_GEOSERVER_VERSION
fi
echo "$PROFILE: GeoServer Cloud $GEOSERVER_TAG / GeoServer $STANDARD_GEOSERVER_VERSION, $POSTGIS_IMAGE"
mkdir -p data  # made by Docker it would be root's, and dataops couldn't write the rasters there
docker compose --profile "$PROFILE" up -d --wait --wait-timeout 900 || { docker compose --profile "$PROFILE" logs "$SERVICE" | tail -50; exit 1; }
if [ "$PROFILE" = standard ]; then
  echo "GeoServer ready at http://localhost:${E2E_STANDARD_PORT:-59091}/geoserver"
else
  echo "GeoServer Cloud ready at http://localhost:${E2E_GATEWAY_PORT:-59090}/geoserver-cloud"
fi
