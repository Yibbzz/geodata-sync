"""The usgs source type (the USGS National Map), against a fake of its API; and reading a COG in place."""

import http.server
import json
import re
import shutil
import subprocess
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from geoserver_dataops.cli import main
from geoserver_dataops.ingest import LoadError
from geoserver_dataops.raster import check_remote_tiff
from geoserver_dataops.sources import SourceError
from geoserver_dataops.sources.usgs import USGS, tile_key
from test_sync_integration import env, schemas  # noqa: F401 (fixtures)

needs_gdal = pytest.mark.skipif(shutil.which("gdal_translate") is None, reason="GDAL's command-line tools not installed")

DATASETS = [
    {"title": "National Elevation Dataset (NED)", "formats": [{"value": "GeoTIFF"}],
     "tags": [{"sbDatasetTag": "National Elevation Dataset (NED) 1/3 arc-second", "tags": []}]},
    {"title": "National Structures Dataset (NSD)", "sbDatasetTag": "National Structures Dataset (NSD)",
     "formats": [{"value": "Shapefile"}, {"value": "GeoPackage"}], "tags": []},
]


class TNM:
    """The products endpoint answers with two editions of tile n40w106, one of n41w106, a page at a time; the files
    are served too, with ranges (GDAL reads COGs in place)."""

    def __init__(self, files: dict[str, bytes] | None = None):
        self.files, self.asked = files or {}, []
        tnm = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                tnm.asked.append((url.path, q))
                if url.path == "/datasets":
                    return self.json(DATASETS)
                if url.path == "/products":
                    items = tnm.items(q)
                    offset, size = int(q.get("offset", 0)), int(q.get("max", 100))
                    return self.json({"total": len(items), "items": items[offset:offset + size]})
                data = tnm.files.get(url.path)
                if data is None:
                    return self.send(404, b"")
                m = re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range", ""))
                if m:
                    start, end = int(m.group(1)), min(int(m.group(2) or len(data) - 1), len(data) - 1)
                    return self.send(206, data[start:end + 1], {"Content-Range": f"bytes {start}-{end}/{len(data)}"})
                self.send(200, data)

            def do_HEAD(self):
                data = tnm.files.get(urlparse(self.path).path)
                self.send(200 if data is not None else 404, b"", {"Content-Length": str(len(data or b""))}, body=False)

            def json(self, value):
                self.send(200, json.dumps(value).encode(), {"Content-Type": "application/json"})

            def send(self, status, data, headers=None, body=True):
                self.send_response(status)
                headers = {"Accept-Ranges": "bytes", **(headers or {})}
                headers.setdefault("Content-Length", str(len(data)))
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                if body:
                    self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def items(self, q: dict) -> list[dict]:
        u = self.url
        if q["datasets"].startswith("National Elevation"):
            return [{"title": "n40w106 2011", "format": "GeoTIFF", "publicationDate": "2011-06-01",
                     "downloadURL": f"{u}/tiles/USGS_13_n40w106_20110601.tif"},
                    {"title": "n40w106 2023", "format": "GeoTIFF", "publicationDate": "2023-06-02",
                     "downloadURL": f"{u}/tiles/USGS_13_n40w106_20230602.tif"},
                    {"title": "n41w106 2022", "format": "GeoTIFF", "publicationDate": "2022-01-01",
                     "downloadURL": f"{u}/tiles/USGS_13_n41w106_20220101.tif"}]
        return [{"title": "Colorado", "format": "GeoPackage", "bestFitIndex": 0.2, "publicationDate": "2025-01-01",
                 "downloadURL": f"{u}/states/STRUCT_CO_State_GPKG.zip"},
                {"title": "District of Columbia", "format": "GeoPackage", "bestFitIndex": 0.9, "publicationDate": "2025-01-01",
                 "downloadURL": f"{u}/states/STRUCT_DC_State_GPKG.zip"}]

    def close(self):
        self.httpd.shutdown()


@pytest.fixture
def tnm():
    t = TNM()
    yield t
    t.close()


def test_the_datasets_are_the_products_and_a_group_without_any_is_one(tnm):
    assert [p.id for p in USGS(url=tnm.url).products().products] == [
        "National Elevation Dataset (NED) 1/3 arc-second", "National Structures Dataset (NSD)"]
    with pytest.raises(SourceError, match="discover usgs` lists them"):
        USGS(url=tnm.url).product("National Structures")


def test_a_raster_selection_is_the_newest_edition_of_each_tile_read_in_place(tnm):
    p = USGS(url=tnm.url).selection("National Elevation Dataset (NED) 1/3 arc-second", {"bbox": [-105.3, 40, -105.25, 40.05]})
    assert [(d.id, d.url.rsplit("/", 1)[-1], d.remote) for d in p.parts] == [
        ("USGS_13_n40w106", "USGS_13_n40w106_20230602.tif", True), ("USGS_13_n41w106", "USGS_13_n41w106_20220101.tif", True)]
    assert p.version.startswith("2 files, ")
    assert tnm.asked[-1][1]["bbox"] == "-105.3,40,-105.25,40.05"


def test_a_vector_selection_is_the_file_that_fits_best(tnm):
    p = USGS(url=tnm.url).selection("National Structures Dataset (NSD)", {"bbox": [-77.05, 38.88, -77, 38.92], "format": "GeoPackage"})
    assert (p.chosen.file_name, p.parts) == ("STRUCT_DC_State_GPKG.zip", [])
    assert tnm.asked[-1][1]["prodFormats"] == "GeoPackage"


def test_tiles_are_told_apart_by_name_without_their_date():
    assert tile_key({"downloadURL": "https://x/USGS_13_n40w106_20110601.tif"}) == "USGS_13_n40w106"
    assert tile_key({"downloadURL": "https://x/current/USGS_13_n40w106.tif"}) == "USGS_13_n40w106"


def test_a_remote_raster_that_isnt_a_tiff_is_refused():
    t = TNM({"/fake.tif": b"<VRTDataset><VRTRasterBand/></VRTDataset>"})
    try:
        with pytest.raises(LoadError, match="isn't a GeoTIFF"):
            check_remote_tiff(f"/vsicurl/{t.url}/fake.tif")
    finally:
        t.close()


@pytest.mark.integration
@needs_gdal
def test_tiles_are_read_in_place_and_cropped(tmp_path, schemas, env, capsys, monkeypatch):
    def cog(west: float) -> bytes:
        asc = tmp_path / f"{west}.asc"
        asc.write_text(f"ncols 100\nnrows 100\nxllcorner {west}\nyllcorner 40\ncellsize 0.01\n" + ("5 " * 100 + "\n") * 100)
        out = tmp_path / f"{west}.tif"
        subprocess.run(["gdal_translate", "-q", "-a_srs", "EPSG:4326", "-of", "COG", str(asc), str(out)], check=True)
        return out.read_bytes()
    t = TNM({"/tiles/USGS_13_n40w106_20230602.tif": cog(-106), "/tiles/USGS_13_n41w106_20220101.tif": cog(-105),
             "/tiles/USGS_13_n40w106_20110601.tif": b"old edition: never read"})
    try:
        monkeypatch.setenv("DATAOPS_DATA_DIR", str(tmp_path))
        (tmp_path / "p").mkdir()
        (tmp_path / "p" / "project.yaml").write_text(f"""\
apiVersion: dataops/v1alpha1
kind: Project
metadata: {{ name: t }}
spec:
  database:
    connection: {{ secret: testdb }}
    schemas: {{ live: {schemas[0]}, staging: {schemas[1]}, backup: {schemas[2]} }}
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: {{ name: tnm }}
spec: {{ type: usgs, url: "{t.url}" }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec:
  source: tnm
  product: National Elevation Dataset (NED) 1/3 arc-second
  usgs: {{ bbox: [-105.5, 40.2, -104.5, 40.4] }}
  raster: {{ path: dem/heights.tif }}
""")
        assert main(["sync", "-C", str(tmp_path / "p"), "sites"]) == 0, capsys.readouterr().err
        assert "(2,000 pixels)" in capsys.readouterr().out  # 1 x 0.2 degrees at 0.01 (100 x 20): across both tiles, cropped
        assert not [p for p, _ in t.asked if p.endswith("20110601.tif")]  # the older edition never fetched
    finally:
        t.close()
