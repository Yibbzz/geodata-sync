"""The data-europa source type, against a fake of data.europa.eu's search API."""

import http.server
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from geoserver_dataops.sources import NotFound
from geoserver_dataops.sources.europa import DataEuropa

DATASET = {
    "id": "sites-geneva", "title": {"fr": "Sites", "en": "Sites of national importance"}, "modified": "2026-10-29T00:00:00",
    "description": {"en": "Points"},
    "distributions": [
        {"id": "d1", "format": {"id": "SHP"}, "title": {"en": "SHP"}, "access_url": ["https://files.example/sites-shp.zip"]},
        {"id": "d2", "format": {"id": "GPKG"}, "title": {"en": "0", "fr": "0"}, "description": {"en": "Sites as a GeoPackage"},
         "download_url": ["https://files.example/sites-gpkg.zip"], "access_url": ["https://portal.example/sites"]},
        {"id": "d3", "format": {"id": "WFS_SRVC"}, "title": {"en": "WFS"},
         "access_url": ["https://ows.example/wfs?service=WFS&request=GetCapabilities"]},
    ],
}


class Portal:
    def __init__(self):
        self.asked = []
        portal = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                portal.asked.append((url.path, {k: v[0] for k, v in parse_qs(url.query).items()}))
                if url.path == "/search":
                    body = {"result": {"count": 1, "results": [DATASET]}}
                elif url.path == "/datasets/sites-geneva":
                    body = {"result": DATASET}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


@pytest.fixture
def portal():
    p = Portal()
    yield p
    p.httpd.shutdown()


def test_the_search_asks_for_geospatial_files_and_lists_datasets(portal):
    listing = DataEuropa(url=portal.url).products("sites", 5)
    assert [(p.id, p.name, p.version, p.loads_as) for p in listing.products] == [
        ("sites-geneva", "Sites of national importance", "2026-10-29T00:00:00", "GeoPackage")]
    query = portal.asked[-1][1]
    assert (query["q"], query["filter"], query["limit"]) == ("sites", "dataset", "5") and '"GPKG"' in query["facets"]


def test_a_dataset_chooses_its_best_file_and_lists_its_services(portal):
    p = DataEuropa(url=portal.url).product("sites-geneva")
    assert p.chosen.url == "https://files.example/sites-gpkg.zip"  # the download address, before the page's
    assert p.chosen.name == "Sites as a GeoPackage"  # titled 0 by its publisher: the description instead
    assert [(d.format, d.loadable) for d in p.downloads] == [("Shapefile", True), ("GeoPackage", True), ("WFS", False)]
    assert DataEuropa(url=portal.url).product("sites-geneva", "d1").chosen.format == "Shapefile"
    with pytest.raises(NotFound):
        DataEuropa(url=portal.url).product("atlantis")
