"""A small catalogue on a local port that answers both OGC API Records and CSW 2.0.2, for the catalogue
sources' tests. Two records: one with a GeoJSON file, a web page and a WFS; one with only an ArcGIS service."""

import http.server
import json
import threading
import urllib.parse

POINTS = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"name": n}, "geometry": {"type": "Point", "coordinates": [i, 52]}}
    for i, n in enumerate("abc")]}


class CatalogueServer:
    def __init__(self, modified: str = "2026-03-01T00:00:00Z"):
        self.modified, self.requests, self.points = modified, [], POINTS
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                server.requests.append(self.path)
                status, kind, body = server.answer(url.path, dict(urllib.parse.parse_qsl(url.query)))
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()

    def records(self):
        b = self.base
        return [
            {"id": "flood-areas", "title": "Flood areas", "description": "Areas at risk of flooding.",
             "links": [{"href": f"{b}/files/flood.geojson", "rel": "enclosure", "type": "application/geo+json", "title": "GeoJSON"},
                       {"href": f"{b}/about.html", "rel": "describedby", "type": "text/html", "title": "About"},
                       {"href": f"{b}/geoserver/wfs", "rel": "service", "type": "OGC:WFS", "title": "Flood areas WFS"}]},
            {"id": "parcels", "title": "Land parcels", "description": "Cadastral parcels.",
             "links": [{"href": "https://gis.example/arcgis/rest/services/Parcels/FeatureServer/0", "rel": "service",
                        "type": "ESRI:REST", "title": "Parcels service"}]},
        ]

    def ogc(self, r):
        return {"type": "Feature", "id": r["id"], "geometry": None,
                "properties": {"title": r["title"], "description": r["description"], "updated": self.modified, "type": "dataset"},
                "links": [{**link, "type": "application/json" if link["type"].startswith("OGC") or ":" in link["type"]
                           and not link["type"].startswith("application") else link["type"]} for link in r["links"]]}

    def csw(self, r):
        uris = "".join(f'<dc:URI protocol="{link["type"]}" name="{link["title"]}">{link["href"]}</dc:URI>' for link in r["links"])
        return (f"<csw:Record><dc:identifier>{r['id']}</dc:identifier><dc:title>{r['title']}</dc:title>"
                f"<dct:abstract>{r['description']}</dct:abstract><dct:modified>{self.modified}</dct:modified>{uris}</csw:Record>")

    def answer(self, path, q):
        if path == "/files/flood.geojson":
            return 200, "application/geo+json", self.points
        if path == "/ogc/collections":
            return 200, "application/json", {"collections": [{"id": "features", "itemType": "feature"},
                                                              {"id": "main", "itemType": "record"}]}
        if path == "/ogc/collections/main/items":
            words = [w.lower() for w in q.get("q", "").split(",") if w]
            found = [r for r in self.records() if all(w in (r["title"] + r["description"]).lower() for w in words)]
            return 200, "application/geo+json", {"type": "FeatureCollection", "numberMatched": len(found),
                                                 "features": [self.ogc(r) for r in found]}
        if path.startswith("/ogc/collections/main/items/"):
            r = next((r for r in self.records() if r["id"] == path.rsplit("/", 1)[1]), None)
            return (200, "application/geo+json", self.ogc(r)) if r else (404, "application/json", {})
        if path == "/csw":
            ns = ('xmlns:csw="http://www.opengis.net/cat/csw/2.0.2" xmlns:dc="http://purl.org/dc/elements/1.1/" '
                  'xmlns:dct="http://purl.org/dc/terms/"')
            if q.get("request") == "GetRecordById":
                r = [r for r in self.records() if r["id"] == q.get("id")]
                return 200, "application/xml", f"<csw:GetRecordByIdResponse {ns}>{''.join(self.csw(x) for x in r)}</csw:GetRecordByIdResponse>".encode()
            if q.get("request") == "GetRecords":
                found = self.records()
                if q.get("constraint"):
                    word = q["constraint"].split("%")[1].lower()
                    found = [r for r in found if word in (r["title"] + r["description"]).lower()]
                records = "".join(self.csw(r) for r in found)
                return 200, "application/xml", (f'<csw:GetRecordsResponse {ns}><csw:SearchResults numberOfRecordsMatched="{len(found)}">'
                                                f"{records}</csw:SearchResults></csw:GetRecordsResponse>").encode()
            return 200, "application/xml", (b'<ows:ExceptionReport xmlns:ows="http://www.opengis.net/ows"><ows:Exception>'
                                            b"<ows:ExceptionText>Operation not supported</ows:ExceptionText></ows:Exception></ows:ExceptionReport>")
        return 404, "text/plain", b"not found"
