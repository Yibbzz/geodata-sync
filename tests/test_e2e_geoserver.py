"""What geoserver-dataops loads, as GeoServer serves it: GeoServer Cloud and standard GeoServer, started by
tests/e2e/up.sh. A table and a raster are loaded, published through GeoServer's REST API, and read back
through WFS and WCS; then new versions are loaded under the running GeoServer and read back again,
after the refresh `configops refresh` does (a raster store's reset, the bounds worked out again).

Needs E2E_GEOSERVER_URL (the REST API, e.g. http://localhost:59091/geoserver/rest), E2E_SERVICES_URL
(where /wfs and /wcs are), E2E_DB_PORT (PostGIS on this machine), E2E_DB_HOST_IN_GEOSERVER (PostGIS as
GeoServer reaches it) and E2E_DATA_DIR (the folder GeoServer has at /data), and GDAL's tools.
"""

import functools
import http.server
import json
import os
import shutil
import subprocess
import threading
import time

import psycopg2
import pytest
import requests

from geoserver_dataops.cli import main

pytestmark = pytest.mark.e2e

if not os.environ.get("E2E_GEOSERVER_URL"):
    pytest.skip("E2E_GEOSERVER_URL not set (tests/e2e/up.sh starts a GeoServer)", allow_module_level=True)
if shutil.which("ogr2ogr") is None:
    pytest.skip("ogr2ogr not installed", allow_module_level=True)

REST = os.environ["E2E_GEOSERVER_URL"].rstrip("/")
SERVICES = os.environ["E2E_SERVICES_URL"].rstrip("/")
AUTH = (os.environ.get("E2E_GEOSERVER_USER", "admin"), os.environ.get("E2E_GEOSERVER_PASSWORD", "geoserver"))
DB = dict(host="localhost", port=int(os.environ.get("E2E_DB_PORT", "55433")), dbname="postgres", user="postgres", password="postgres")
DB_IN_GEOSERVER = os.environ.get("E2E_DB_HOST_IN_GEOSERVER", "db")
DATA = os.path.abspath(os.environ.get("E2E_DATA_DIR", "tests/e2e/data"))
WS, LIVE = "dataops_e2e", "dataops_e2e"
RASTER = "dataops-e2e/dtm.tif"  # under the data folder: /data/dataops-e2e/dtm.tif to GeoServer


def points(names):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": n}, "geometry": {"type": "Point", "coordinates": [400000 + i * 100, 200000]}}
        for i, n in enumerate(names)]}


def grid(values):
    """A 4 x 4 ASCII grid (no CRS of its own: the Dataset gives EPSG:27700), one value per row."""
    rows = "".join(f"{v} {v} {v} {v}\n" for v in values)
    return f"ncols 4\nnrows 4\nxllcorner 400000\nyllcorner 200000\ncellsize 50\n{rows}"


def publish_files(www, names, values):
    """sites.gpkg (layer `points`, EPSG:27700) and dtm.asc, as a new version of each would be."""
    src = www / "points.geojson"
    src.write_text(json.dumps(points(names)))
    (www / "sites.gpkg").unlink(missing_ok=True)
    subprocess.run(["ogr2ogr", "-f", "GPKG", str(www / "sites.gpkg"), str(src), "-nln", "points", "-a_srs", "EPSG:27700"],
                   check=True, capture_output=True)
    (www / "dtm.asc").write_text(grid(values))
    time.sleep(1.1)  # a new Last-Modified, which is how a url source sees a new version


def project(root, base):
    (root / "project.yaml").write_text(f"""\
apiVersion: dataops/v1alpha1
kind: Project
metadata: {{ name: e2e }}
spec:
  database:
    connection: {{ secret: e2edb }}
    schemas: {{ live: {LIVE}, staging: {LIVE}_staging, backup: {LIVE}_backup }}
  backups: 1
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: {{ name: web }}
spec: {{ type: url }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec: {{ source: web, url: "{base}/sites.gpkg" }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: dtm }}
spec:
  source: web
  url: "{base}/dtm.asc"
  raster: {{ path: {RASTER}, crs: "EPSG:27700" }}
""")
    return root


def rest(method, path, body=None, ok=(200, 201), **params):
    r = requests.request(method, f"{REST}/{path}", auth=AUTH, json=body, params=params, timeout=60,
                         headers={"Accept": "application/json"})
    assert r.status_code in ok, f"{method} {path}: HTTP {r.status_code}: {r.text[:500]}"
    return r


def eventually(check, expected, seconds=90):
    """What `check` gives, once it gives `expected`: GeoServer Cloud's services learn of a catalog change
    through its event bus, a moment after the REST service makes it."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            got = check()
            if got == expected or time.monotonic() > deadline:
                return got
        except AssertionError:
            if time.monotonic() > deadline:
                raise
        time.sleep(2)


def wfs_names():
    r = requests.get(f"{SERVICES}/wfs", params={
        "service": "WFS", "version": "2.0.0", "request": "GetFeature", "typeNames": f"{WS}:sites_points",
        "outputFormat": "application/json", "srsName": "EPSG:27700"}, auth=AUTH, timeout=60)
    assert r.status_code == 200 and r.headers.get("Content-Type", "").startswith("application/json"), r.text[:500]
    features = r.json()["features"]
    assert all(f["geometry"]["type"] in ("Point", "MultiPoint") for f in features)
    return sorted(f["properties"]["name"] for f in features)


def wcs_values(tmp_path):
    """The (min, max) of the raster as GeoServer's WCS sends it, and its size."""
    r = requests.get(f"{SERVICES}/wcs", params={
        "service": "WCS", "version": "2.0.1", "request": "GetCoverage", "coverageId": f"{WS}__dtm", "format": "image/tiff"},
        auth=AUTH, timeout=60)
    assert r.status_code == 200 and r.headers.get("Content-Type", "").startswith("image/tiff"), r.text[:500]
    path = tmp_path / f"wcs-{time.monotonic_ns()}.tif"
    path.write_bytes(r.content)
    info = json.loads(subprocess.run(["gdalinfo", "-json", "-stats", str(path)], check=True, capture_output=True, text=True).stdout)
    band = info["bands"][0]
    return tuple(info["size"]), (band["minimum"], band["maximum"])


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A web server for the downloads, a clean database and workspace, and the database secret."""
    www = tmp_path / "www"
    www.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(www))
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    rest("DELETE", f"workspaces/{WS}", ok=(200, 404), recurse="true")
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        for schema in (LIVE, f"{LIVE}_staging", f"{LIVE}_backup"):
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        cur.execute("SELECT to_regclass('public.os_ingest_log')")
        if cur.fetchone()[0]:
            cur.execute("DELETE FROM public.os_ingest_log WHERE dataset_id IN ('sites', 'dtm')")
    shutil.rmtree(os.path.join(DATA, "dataops-e2e"), ignore_errors=True)
    monkeypatch.setenv("DATAOPS_DATA_DIR", DATA)
    for key, value in {"HOST": "localhost", "PORT": str(DB["port"]), "DATABASE": "postgres",
                       "USERNAME": "postgres", "PASSWORD": "postgres"}.items():
        monkeypatch.setenv(f"DATAOPS_E2EDB_{key}", value)
    yield www, f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_geoserver_serves_what_dataops_loads_and_reloads(setup, tmp_path, capsys):
    www, base = setup
    root = tmp_path / "project"
    root.mkdir()
    project(root, base)

    # 1. The first versions, loaded
    publish_files(www, ["a", "b", "c"], [1, 2, 3, 4])
    assert main(["sync", "-C", str(root), "sites", "dtm"]) == 0, capsys.readouterr().err
    assert os.path.isfile(os.path.join(DATA, RASTER))

    # 2. Published through the REST API, as GeoServer ConfigOps would
    rest("POST", "workspaces", {"workspace": {"name": WS}})
    params = {"dbtype": "postgis", "host": DB_IN_GEOSERVER, "port": "5432", "database": "postgres",
              "user": "postgres", "passwd": "postgres", "schema": LIVE}
    rest("POST", f"workspaces/{WS}/datastores", {"dataStore": {
        "name": "pg", "connectionParameters": {"entry": [{"@key": k, "$": v} for k, v in params.items()]}}})
    rest("POST", f"workspaces/{WS}/datastores/pg/featuretypes", {"featureType": {"name": "sites_points", "nativeName": "sites_points"}})
    rest("POST", f"workspaces/{WS}/coveragestores", {"coverageStore": {
        "name": "dtm", "type": "GeoTIFF", "enabled": True, "workspace": WS, "url": f"file:/data/{RASTER}"}})
    rest("POST", f"workspaces/{WS}/coveragestores/dtm/coverages", {"coverage": {"name": "dtm", "nativeName": "dtm"}})

    feature_type = rest("GET", f"workspaces/{WS}/datastores/pg/featuretypes/sites_points").json()["featureType"]
    assert feature_type["srs"] == "EPSG:27700"
    coverage = rest("GET", f"workspaces/{WS}/coveragestores/dtm/coverages/dtm").json()["coverage"]
    assert coverage["srs"] == "EPSG:27700"

    # 3. Served as loaded: the features, and the pixels of the Cloud-Optimised GeoTIFF
    assert eventually(wfs_names, ["a", "b", "c"]) == ["a", "b", "c"]
    assert eventually(lambda: wcs_values(tmp_path), ((4, 4), (1.0, 4.0))) == ((4, 4), (1.0, 4.0))

    # 4. New versions loaded under the running GeoServer: the table swapped in, the COG renamed over the old one
    publish_files(www, ["a", "b", "c", "d"], [10, 20, 30, 40])
    assert main(["sync", "-C", str(root), "sites", "dtm"]) == 0, capsys.readouterr().err
    out = capsys.readouterr().out
    assert "sites: loaded" in out and "dtm: loaded" in out

    # 5. What `configops refresh` then does: reset the raster store so GeoServer reads the new file, bounds again
    rest("POST", f"workspaces/{WS}/coveragestores/dtm/reset")
    rest("PUT", f"workspaces/{WS}/coveragestores/dtm/coverages/dtm", {"coverage": {"enabled": True}}, calculate="nativebbox,latlonbbox")
    rest("PUT", f"workspaces/{WS}/datastores/pg/featuretypes/sites_points", {"featureType": {"enabled": True}},
         recalculate="nativebbox,latlonbbox")

    assert eventually(wfs_names, ["a", "b", "c", "d"]) == ["a", "b", "c", "d"]
    assert eventually(lambda: wcs_values(tmp_path), ((4, 4), (10.0, 40.0))) == ((4, 4), (10.0, 40.0))
