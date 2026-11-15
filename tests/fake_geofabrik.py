"""A small Geofabrik on a local port, for the geofabrik source's tests: an index of three regions (one with
the free shapefiles and an OSM extract, one with only the extract, as large regions are), the files, their
.md5 files, and Last-Modified for the version."""

import hashlib
import http.server
import json
import re
import subprocess
import threading
import zipfile
from email.utils import formatdate
from pathlib import Path

OSM = """<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="test">
  <node id="1" lat="51.50" lon="-0.12" version="1"><tag k="amenity" v="cafe"/><tag k="name" v="Cafe A"/></node>
  <node id="2" lat="51.51" lon="-0.13" version="1"/>
  <node id="3" lat="51.52" lon="-0.14" version="1"/>
  <way id="10" version="1"><nd ref="2"/><nd ref="3"/><tag k="highway" v="residential"/><tag k="name" v="High Street"/></way>
</osm>
"""


def shapefile_pack(folder: Path, cafes: int = 2) -> bytes:
    """A zip as Geofabrik's free shapefiles: gis_osm_roads_free_1 and gis_osm_pois_free_1 (`cafes` of them)."""
    layers = {"gis_osm_roads_free_1": [("High Street", "LineString", [[-0.13, 51.51], [-0.14, 51.52]])],
              "gis_osm_pois_free_1": [(f"Cafe {i}", "Point", [-0.12 + i / 100, 51.50]) for i in range(cafes)]}
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "pack.zip"
    with zipfile.ZipFile(out, "w") as z:
        for layer, rows in layers.items():
            src = folder / f"{layer}.geojson"
            src.write_text(json.dumps({"type": "FeatureCollection", "features": [
                {"type": "Feature", "properties": {"name": n}, "geometry": {"type": t, "coordinates": c}} for n, t, c in rows]}))
            subprocess.run(["ogr2ogr", "-f", "ESRI Shapefile", str(folder / f"{layer}.shp"), str(src), "-nln", layer],
                           check=True, capture_output=True)
            for ext in (".shp", ".shx", ".dbf", ".prj"):
                z.write(folder / f"{layer}{ext}", f"{layer}{ext}")
    return out.read_bytes()


class GeofabrikServer:
    def __init__(self, folder: Path):
        self.built, self.requests = 1_760_000_000, []
        self.files = {"/europe/test-latest-free.shp.zip": shapefile_pack(folder),
                      "/europe/test-latest.osm.pbf": OSM.encode(),
                      "/europe/big-latest.osm.pbf": OSM.encode()}
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server.requests.append((self.path, self.headers.get("Range")))
                if self.path == "/index-v1-nogeom.json":
                    return self.send(200, json.dumps(server.index()).encode(), {})
                if self.path.endswith(".md5"):
                    data = server.files.get(self.path.removesuffix(".md5"))
                    if data is None:
                        return self.send(404, b"", {})
                    return self.send(200, f"{hashlib.md5(data).hexdigest()}  {self.path.rsplit('/', 1)[1]}\n".encode(), {})
                data = server.files.get(self.path)
                if data is None:
                    return self.send(404, b"", {})
                headers = {"Last-Modified": formatdate(server.built, usegmt=True)}
                m = re.match(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
                if m:
                    start, end = int(m.group(1)), min(int(m.group(2)), len(data) - 1)
                    return self.send(206, data[start:end + 1], {**headers, "Content-Range": f"bytes {start}-{end}/{len(data)}"})
                self.send(200, data, headers)

            def send(self, status, body, headers):
                self.send_response(status)
                for k, v in {"Content-Length": str(len(body)), **headers}.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()

    def index(self):
        u = self.url
        return {"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"id": "europe", "name": "Europe", "urls": {}}},
            {"type": "Feature", "properties": {"id": "test", "parent": "europe", "name": "Testshire",
                                               "urls": {"pbf": f"{u}/europe/test-latest.osm.pbf",
                                                        "shp": f"{u}/europe/test-latest-free.shp.zip"}}},
            {"type": "Feature", "properties": {"id": "big", "parent": "europe", "name": "Bigland",
                                               "urls": {"pbf": f"{u}/europe/big-latest.osm.pbf"}}}]}

    def downloads(self):
        return [p for p, rng in self.requests if rng is None and not p.endswith((".md5", ".json"))]
