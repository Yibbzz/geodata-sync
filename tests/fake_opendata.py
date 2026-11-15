"""Small Socrata and OpenDataSoft portals on a local port, for their sources' tests: each has a dataset
with geometry, one without, and a GeoJSON export of the first."""

import http.server
import json
import threading
import urllib.parse

POINTS = {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"name": n}, "geometry": {"type": "Point", "coordinates": [-73.9 + i / 100, 40.7]}}
    for i, n in enumerate("abcd")]}


class PortalServer:
    def __init__(self):
        self.updated, self.points, self.requests = "2026-02-01T00:00:00.000Z", POINTS, []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                server.requests.append((self.path, dict(self.headers)))
                status, body = server.answer(url.path, dict(urllib.parse.parse_qsl(url.query)))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()

    def socrata(self):
        return [{"id": "trees-2015", "name": "Street trees", "type": "dataset", "description": "Every street tree.",
                 "data_updated_at": self.updated, "updatedAt": "2026-03-01T00:00:00.000Z",
                 "columns_datatype": ["Text", "Number", "Point"]},
                {"id": "boro-bnds", "name": "Borough boundaries", "type": "map", "data_updated_at": self.updated},
                {"id": "311-calls", "name": "311 calls", "type": "dataset", "columns_datatype": ["Text", "Number"]}]

    def ods(self, d):
        return {"dataset_id": d, "features": ["geo", "analyze"] if d != "budget" else ["analyze"],
                "metas": {"default": {"title": {"arbres": "Arbres", "budget": "Budget"}[d], "description": "Les arbres.",
                                      "modified": "2026-03-01T00:00:00+00:00", "data_processed": self.updated}}}

    def answer(self, path, q):
        if path == "/api/catalog/v1":
            found = self.socrata()
            if q.get("ids"):
                found = [r for r in found if r["id"] == q["ids"]]
            if q.get("q"):
                found = [r for r in found if q["q"].lower() in r["name"].lower()]
            return 200, {"results": [{"resource": r, "metadata": {"domain": "127.0.0.1"}} for r in found],
                         "resultSetSize": len(found)}
        if path in ("/resource/trees-2015.geojson", "/api/geospatial/boro-bnds"):
            return 200, self.points
        api = "/api/explore/v2.1/catalog/datasets"
        if path == api:
            found = ["arbres"] if 'features = "geo"' in q.get("where", "") else ["arbres", "budget"]
            if "search(" in q.get("where", "") and 'search("arbres")' not in q["where"]:
                found = []
            return 200, {"total_count": len(found), "results": [self.ods(d) for d in found]}
        if path in (f"{api}/arbres", f"{api}/budget"):
            return 200, self.ods(path.rsplit("/", 1)[1])
        if path == f"{api}/arbres/exports/geojson":
            return 200, self.points
        return 404, {"error": "not found"}
