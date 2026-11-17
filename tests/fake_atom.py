"""A small INSPIRE Atom download service on a local port, for the atom source's tests: a service feed with
two datasets, one whose feed offers a GeoPackage and a GML (in two CRSs), one whose feed has no file dataops
loads, and the files themselves. `updated` is the version; changing it is a new version."""

import http.server
import json
import subprocess
import threading
from pathlib import Path


def geopackage(folder: Path, sites: int = 2) -> bytes:
    folder.mkdir(parents=True, exist_ok=True)
    src, out = folder / "sites.geojson", folder / "sites.gpkg"
    out.unlink(missing_ok=True)
    src.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"name": f"Site {i}"}, "geometry": {"type": "Point", "coordinates": [5 + i / 100, 52]}}
        for i in range(sites)]}))
    subprocess.run(["ogr2ogr", "-f", "GPKG", str(out), str(src), "-nln", "protected_sites"], check=True, capture_output=True)
    return out.read_bytes()


class AtomServer:
    def __init__(self, folder: Path):
        self.updated, self.requests = "2026-06-03T12:00:00Z", []
        self.files = {"/downloads/sites.gpkg": geopackage(folder)}
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server.requests.append(self.path)
                feeds = {"/atom/index.xml": server.service_feed(), "/atom/protected_sites.xml": server.dataset_feed(),
                         "/atom/maps_only.xml": server.maps_feed()}
                if self.path in feeds:
                    return self.send(200, feeds[self.path].encode(), "application/atom+xml")
                if self.path in server.files:
                    return self.send(200, server.files[self.path], "application/geopackage+sqlite3")
                self.send(404, b"", "text/plain")

            def send(self, status, body, kind):
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()

    def service_feed(self) -> str:
        u = self.url
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:inspire_dls="http://inspire.ec.europa.eu/schemas/inspire_dls/1.0">
  <id>{u}/atom/index.xml</id>
  <title>Test download service</title>
  <updated>{self.updated}</updated>
  <entry>
    <id>{u}/atom/protected_sites.xml</id>
    <title>Protected sites</title>
    <summary>Nature reserves of Testland</summary>
    <link href="{u}/atom/protected_sites.xml" rel="alternate" type="application/atom+xml"/>
    <link href="https://catalogue.example/record/1" rel="describedby" type="application/xml"/>
    <updated>{self.updated}</updated>
  </entry>
  <entry>
    <id>{u}/atom/maps_only.xml</id>
    <title>Scanned maps</title>
    <link href="{u}/atom/maps_only.xml" rel="alternate" type="application/atom+xml"/>
    <updated>2020-01-01T00:00:00Z</updated>
  </entry>
</feed>"""

    def dataset_feed(self) -> str:
        u = self.url
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>{u}/atom/protected_sites.xml</id>
  <title>Protected sites</title>
  <link href="{u}/atom/index.xml" rel="up" type="application/atom+xml"/>
  <updated>{self.updated}</updated>
  <entry>
    <id>{u}/atom/protected_sites_gml.xml</id>
    <title>Protected sites (GML)</title>
    <link href="{u}/downloads/sites.gml" rel="alternate" type="application/gml+xml" length="900"/>
    <category term="http://www.opengis.net/def/crs/EPSG/0/4258" label="ETRS89"/>
  </entry>
  <entry>
    <id>{u}/atom/protected_sites_gpkg.xml</id>
    <title>Protected sites (GeoPackage)</title>
    <link href="{u}/downloads/sites.gpkg" rel="alternate" type="application/geopackage+sqlite3" length="98304"
          title="sites.gpkg"/>
    <category term="http://www.opengis.net/def/crs/EPSG/0/4326" label="WGS 84"/>
  </entry>
</feed>"""

    def maps_feed(self) -> str:
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>{self.url}/atom/maps_only.xml</id>
  <title>Scanned maps</title>
  <updated>2020-01-01T00:00:00Z</updated>
  <entry>
    <id>map1</id>
    <title>Sheet 1</title>
    <link href="{self.url}/downloads/sheet1.png" rel="alternate" type="image/png"/>
  </entry>
</feed>"""

    def downloads(self):
        return [p for p in self.requests if p.startswith("/downloads/")]
