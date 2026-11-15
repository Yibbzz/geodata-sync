"""A small STAC API on a local port, for the stac source's tests: one collection of DEM tiles (two GeoTIFFs
side by side, made with GDAL) whose search pages two items at a time, by a GET link or a POST one."""

import http.server
import json
import subprocess
import threading
import urllib.parse
from pathlib import Path


def tile(folder: Path, name: str, x: float, value: int) -> Path:
    """A 2 x 2 GeoTIFF in degrees at (x, 50), every pixel `value`."""
    grid = folder / f"{name}.asc"
    grid.write_text(f"ncols 2\nnrows 2\nxllcorner {x}\nyllcorner 50\ncellsize 0.5\n{value} {value}\n{value} {value}\n")
    out = folder / f"{name}.tif"
    subprocess.run(["gdal_translate", "-q", "-a_srs", "EPSG:4326", str(grid), str(out)], check=True, capture_output=True)
    return out


class STACServer:
    def __init__(self, folder: Path, post_paging: bool = False):
        self.folder, self.post_paging, self.requests = folder, post_paging, []
        self.items = []
        for i, (x, value) in enumerate([(0.0, 10), (1.0, 20), (2.0, 30)]):
            tile(folder, f"tile{i}", x, value)
            self.items.append({"id": f"dem-{i}", "x": x, "updated": "2026-01-01T00:00:00Z"})
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                server.requests.append(("GET", self.path))
                if url.path.startswith("/files/"):
                    return self.send(200, (server.folder / url.path.removeprefix("/files/")).read_bytes(), "image/tiff")
                self.json(*server.answer(url.path, dict(urllib.parse.parse_qsl(url.query))))

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                server.requests.append(("POST", self.path, body))
                self.json(*server.answer(urllib.parse.urlparse(self.path).path, body))

            def json(self, status, body):
                self.send(status, json.dumps(body).encode(), "application/geo+json")

            def send(self, status, data, kind):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def close(self):
        self.httpd.shutdown()

    def item(self, i: dict) -> dict:
        n = i["id"].split("-")[1]
        return {"type": "Feature", "stac_version": "1.0.0", "id": i["id"], "collection": "dem",
                "bbox": [i["x"], 50, i["x"] + 1, 51], "geometry": None,
                "properties": {"datetime": "2021-04-22T00:00:00Z", "updated": i["updated"]},
                "assets": {"data": {"href": f"{self.url.removesuffix('/v1')}/files/tile{n}.tif",
                                    "type": "image/tiff; application=geotiff; profile=cloud-optimized", "roles": ["data"]},
                           "metadata": {"href": f"{self.url}/meta/{n}.xml", "type": "application/xml", "roles": ["metadata"]}}}

    def answer(self, path: str, q: dict):
        if path == "/v1/collections":
            return 200, {"collections": [{"id": "dem", "title": "Copernicus DEM", "description": "Elevation."},
                                         {"id": "sentinel-2", "title": "Sentinel-2"}], "links": []}
        if path == "/v1/collections/dem":
            return 200, {"id": "dem", "title": "Copernicus DEM", "description": "Elevation.",
                         "extent": {"spatial": {"bbox": [[-180, -90, 180, 90]]}},
                         "item_assets": {"data": {"type": "image/tiff; application=geotiff; profile=cloud-optimized",
                                                  "roles": ["data"], "title": "Heights"},
                                         "metadata": {"type": "application/xml", "roles": ["metadata"]}}}
        if path != "/v1/search":
            return 404, {"code": "NotFound"}
        bbox = q["bbox"] if isinstance(q.get("bbox"), list) else [float(v) for v in q["bbox"].split(",")]
        wanted = [i for i in self.items if i["x"] < bbox[2] and i["x"] + 1 > bbox[0]]
        start = int(q.get("token", 0))
        page = wanted[start:start + 2]
        links = []
        if start + 2 < len(wanted):
            if self.post_paging:
                links.append({"rel": "next", "href": f"{self.url}/search", "method": "POST", "body": {"token": start + 2}})
            else:
                links.append({"rel": "next", "href": f"{self.url}/search?collections=dem&bbox={q['bbox']}&limit=2&token={start + 2}"})
        return 200, {"type": "FeatureCollection", "features": [self.item(i) for i in page], "links": links}
