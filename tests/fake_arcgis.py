"""A small ArcGIS REST API on a local port, for the arcgis source's tests: a services directory with a
folder, a FeatureServer (two layers, a group layer and a table), the same service as a MapServer,
a token-protected service, and a query endpoint that pages as ArcGIS does."""

import http.server
import json
import threading
import urllib.parse

PAGE = 10  # the server's maxRecordCount


class ArcGISServer:
    def __init__(self, rivers: int = 25, edited: int | None = 1_760_000_000_000):
        self.rivers, self.edited, self.requests = rivers, edited, []
        self.required_token = None  # set: the Hydro service needs this token, which the token endpoint gives user1/pw1
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                server.requests.append(self.path)
                status, body = server.answer(url.path, dict(urllib.parse.parse_qsl(url.query)))
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
                server.requests.append(self.path)
                ok = self.path == "/arcgis/tokens/generateToken" and (form.get("username"), form.get("password")) == ("user1", "pw1")
                body = {"token": server.required_token, "expires": 0} if ok else \
                    {"error": {"code": 400, "message": "Unable to generate token.", "details": ["Invalid username or password."]}}
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.root = f"http://127.0.0.1:{self.httpd.server_address[1]}/arcgis/rest/services"

    def close(self):
        self.httpd.shutdown()

    def layer(self, i: int) -> dict:
        common = {"currentVersion": 11.3, "capabilities": "Query,Extract", "objectIdField": "OBJECTID",
                  "fields": [{"name": "OBJECTID", "type": "esriFieldTypeOID"},
                             {"name": "name", "type": "esriFieldTypeString", "length": 40}]}
        if i == 0:
            info = {"id": 0, "name": "Rivers", "type": "Feature Layer", "geometryType": "esriGeometryPolyline",
                    "description": "Main rivers.", "extent": {"spatialReference": {"wkid": 27700, "latestWkid": 27700}}}
            if self.edited:
                info["editingInfo"] = {"lastEditDate": self.edited, "dataLastEditDate": self.edited}
            return {**common, **info}
        if i == 1:
            return {**common, "id": 1, "name": "Lakes", "type": "Feature Layer", "geometryType": "esriGeometryPolygon",
                    "extent": {"spatialReference": {"wkid": 102100, "latestWkid": 3857}},
                    "drawingInfo": {"renderer": {"type": "simple", "symbol": {
                        "type": "esriSFS", "style": "esriSFSSolid", "color": [0, 92, 230, 255],
                        "outline": {"type": "esriSLS", "style": "esriSLSSolid", "color": [0, 0, 0, 255], "width": 0.75}}},
                        "transparency": 0}}
        if i == 3:
            return {**common, "id": 3, "name": "Gauges", "type": "Table"}
        return {"id": 2, "name": "Water", "type": "Group Layer", "subLayerIds": [0, 1]}

    def features(self, offset: int, count: int) -> list[dict]:
        return [{"attributes": {"OBJECTID": i + 1, "name": f"river {i + 1}"},
                 "geometry": {"paths": [[[400000 + i, 200000], [400100 + i, 200100]]]}}
                for i in range(offset, min(offset + count, self.rivers))]

    def answer(self, path: str, q: dict) -> tuple[int, dict]:
        base = "/arcgis/rest/services"
        if path == "/arcgis/rest/info":
            return 200, {"authInfo": {"isTokenBasedSecurity": True, "tokenServicesUrl": self.root.replace("/rest/services", "/tokens/")}}
        if not path.startswith(base):
            return 404, {}
        if self.required_token and path.startswith(f"{base}/Hydro"):
            if not q.get("token"):
                return 200, {"error": {"code": 499, "message": "Token Required", "details": []}}
            if q["token"] != self.required_token:
                return 200, {"error": {"code": 498, "message": "Invalid Token", "details": []}}
        rest = path[len(base):].strip("/")
        if rest == "":
            return 200, {"currentVersion": 11.3, "folders": ["Hydro"],
                         "services": [{"name": "Basemap", "type": "MapServer"}, {"name": "Private", "type": "FeatureServer"},
                                      {"name": "Geocoder", "type": "GeocodeServer"}]}
        if rest == "Hydro":
            return 200, {"folders": [], "services": [{"name": "Hydro/Water", "type": "FeatureServer"},
                                                     {"name": "Hydro/Water", "type": "MapServer"}]}
        if rest.startswith("Private"):
            return 200, {"error": {"code": 499, "message": "Token Required", "details": []}}
        if rest in ("Hydro/Water/FeatureServer", "Hydro/Water/MapServer"):
            return 200, {"layers": [self.layer(0), self.layer(1), self.layer(2)], "tables": [self.layer(3)]}
        if rest == "Basemap/MapServer":
            return 200, {"layers": [{"id": 0, "name": "Labels"}]}
        prefix = "Hydro/Water/FeatureServer/"
        if not rest.startswith(prefix):
            return 200, {"error": {"code": 400, "message": "Service not found", "details": []}}
        layer, _, op = rest[len(prefix):].partition("/")
        if layer not in ("0", "1", "2", "3"):
            return 200, {"error": {"code": 400, "message": "Invalid or missing input parameters.", "details": []}}
        if not op:
            return 200, self.layer(int(layer))
        if q.get("returnCountOnly") == "true":
            return 200, {"count": self.rivers}
        if q.get("returnExtentOnly") == "true":
            return 200, {"extent": {"xmin": 400000, "ymin": 200000, "xmax": 400200, "ymax": 200200,
                                    "spatialReference": {"wkid": 27700}}}
        offset, count = int(q.get("resultOffset", 0)), min(int(q.get("resultRecordCount", PAGE)), PAGE)
        return 200, {"objectIdFieldName": "OBJECTID", "geometryType": "esriGeometryPolyline",
                     "spatialReference": {"wkid": 27700, "latestWkid": 27700},
                     "fields": self.layer(0)["fields"], "features": self.features(offset, count),
                     "exceededTransferLimit": offset + count < self.rivers}
