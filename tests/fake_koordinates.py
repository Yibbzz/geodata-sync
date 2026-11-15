"""A small Koordinates portal on a local port, for the koordinates source's tests: layers to search, an
export made after one poll, and its download redirected to "storage" on another host that wants no key.
Every API request must carry the key; the storage records whether one was sent to it."""

import http.server
import json
import subprocess
import threading
import zipfile
from pathlib import Path

KEY = "key s3cr3t"


def gpkg_zip(folder: Path, names) -> bytes:
    folder.mkdir(parents=True, exist_ok=True)
    src = folder / "parcels.geojson"
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"parcel": n}, "geometry": {"type": "Point", "coordinates": [1750000 + i, 5920000]}}
        for i, n in enumerate(names)]}))
    gpkg = folder / "nz-parcels.gpkg"
    gpkg.unlink(missing_ok=True)
    subprocess.run(["ogr2ogr", "-f", "GPKG", str(gpkg), str(src), "-nln", "nz_parcels", "-a_srs", "EPSG:2193"],
                   check=True, capture_output=True)
    out = folder / "export.zip"
    with zipfile.ZipFile(out, "w") as z:
        z.write(gpkg, "nz-parcels/nz-parcels.gpkg")
    return out.read_bytes()


class Server:
    def __init__(self, handler):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


class KoordinatesPortal:
    def __init__(self, folder: Path):
        self.version, self.exports, self.unauthorised, self.storage_auth = 1001, [], [], []
        self.file = gpkg_zip(folder, "abc")
        portal = self

        def send(h, status, body, kind="application/json", headers=None):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            h.send_response(status)
            for k, v in {"Content-Type": kind, "Content-Length": str(len(data)), **(headers or {})}.items():
                h.send_header(k, v)
            h.end_headers()
            h.wfile.write(data)

        class Storage(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                portal.storage_auth.append(self.headers.get("Authorization"))
                send(self, 200, portal.file, "application/zip")

            def log_message(self, *args):
                pass

        class Portal(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.answer("GET", None)

            def do_POST(self):
                self.answer("POST", json.loads(self.rfile.read(int(self.headers["Content-Length"]))))

            def answer(self, method, body):
                if self.headers.get("Authorization") != KEY:
                    portal.unauthorised.append(self.path)
                    return send(self, 403, {"detail": "Authentication credentials were not provided."})
                status, out, headers = portal.answer(method, self.path, body)
                send(self, status, out, headers=headers)

            def log_message(self, *args):
                pass

        self.storage = Server(Storage)
        self.portal = Server(Portal)
        self.url = self.portal.url

    def close(self):
        self.storage.httpd.shutdown()
        self.portal.httpd.shutdown()

    def layer(self, lid):
        layers = {"50772": {"title": "NZ Primary Parcels", "kind": "vector",
                            "data": {"crs": {"id": "EPSG:2193"}, "geometry_type": "multipolygon"}},
                  "51000": {"title": "NZ Aerial Imagery", "kind": "raster"}}
        if lid not in layers:
            return None
        return {"id": int(lid), "description": "Land parcels.", "version": {"id": self.version},
                "published_at": "2026-01-01T00:00:00Z", **layers[lid]}

    def answer(self, method, path, body):
        api = "/services/api/v1.x"
        if method == "GET" and path.startswith(f"{api}/layers/?"):
            found = [self.layer("50772")] if "parcels" in path.lower() or "q=" not in path else []
            return 200, found, {"X-Resource-Range": f"0-{len(found)}/{len(found)}"}
        if method == "GET" and path.startswith(f"{api}/layers/"):
            layer = self.layer(path.split("/")[-2])
            return (200, layer, {}) if layer else (404, {"detail": "Not found."}, {})
        if method == "POST" and path == f"{api}/exports/":
            eid = len(self.exports) + 1
            self.exports.append({"id": eid, "body": body, "polls": 0})
            return 201, {"id": eid, "state": "processing", "url": f"{self.url}{api}/exports/{eid}/"}, {}
        if method == "GET" and path.startswith(f"{api}/exports/") and path.endswith("/download/"):
            return 302, {}, {"Location": f"{self.storage.url}/signed/export.zip?X-Amz-Signature=abc"}
        if method == "GET" and path.startswith(f"{api}/exports/"):
            export = self.exports[int(path.split("/")[-2]) - 1]
            export["polls"] += 1
            return 200, {"id": export["id"], "state": "complete",
                         "download_url": f"{self.url}{api}/exports/{export['id']}/download/"}, {}
        return 404, {"detail": "Not found."}, {}
