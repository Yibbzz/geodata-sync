"""The geonode source type, against a fake of GeoNode's API (/api/v2/datasets); and an OGC exception report
in place of a download, which says what the server said."""

import http.server
import json
import threading
from urllib.parse import parse_qs, urlparse

import pytest

from geoserver_dataops.remote import service_error
from geoserver_dataops.sources import NotFound
from geoserver_dataops.sources.geonode import GeoNode


def dataset(base: str, name: str) -> dict:
    wfs = f"{base}/geoserver/ows?service=WFS&version=1.0.0&request=GetFeature&typename=geonode%3A{name}"
    return {"pk": 7, "alternate": f"geonode:{name}", "title": "Rings", "last_updated": "2026-10-22T10:00:00Z", "abstract": "<p>Rings</p>",
            "links": [{"link_type": "data", "extension": "excel", "name": "Excel", "url": f"{wfs}&outputFormat=excel"},
                      {"link_type": "data", "extension": "zip", "name": "Zipped Shapefile", "url": f"{wfs}&outputFormat=SHAPE-ZIP"},
                      {"link_type": "data", "extension": "json", "name": "GeoJSON", "url": f"{wfs}&outputFormat=json"},
                      {"link_type": "data", "extension": "shx", "name": "Original", "url": f"{base}/api/v2/assets/1/link"},
                      {"link_type": "OGC:WFS", "extension": "html", "name": "OGC WFS", "url": f"{base}/geoserver/ows"},
                      {"link_type": "OGC:WMS", "extension": "html", "name": "OGC WMS", "url": f"{base}/geoserver/ows"}]}


# What GeoServer's WMS GetStyles answers for a layer with two styles, as GeoNode's GeoServer gives it
GET_STYLES = """<?xml version="1.0" encoding="UTF-8"?><sld:StyledLayerDescriptor xmlns:sld="http://www.opengis.net/sld"
 xmlns:ogc="http://www.opengis.net/ogc" xmlns:gml="http://www.opengis.net/gml" version="1.0.0">
<sld:NamedLayer><sld:Name>geonode:rings</sld:Name>
<sld:UserStyle><sld:Name>rings_by_kind</sld:Name><sld:Title>By kind</sld:Title><sld:FeatureTypeStyle><sld:Rule>
<ogc:Filter><ogc:PropertyIsEqualTo><ogc:PropertyName>Kind</ogc:PropertyName><ogc:Literal>A</ogc:Literal></ogc:PropertyIsEqualTo></ogc:Filter>
<sld:PolygonSymbolizer><sld:Fill><sld:CssParameter name="fill">#ff0000</sld:CssParameter></sld:Fill></sld:PolygonSymbolizer>
</sld:Rule></sld:FeatureTypeStyle></sld:UserStyle>
<sld:UserStyle><sld:Name>rings</sld:Name><sld:Title>Default Polygon</sld:Title><sld:IsDefault>1</sld:IsDefault><sld:FeatureTypeStyle>
<sld:Rule><sld:PolygonSymbolizer><sld:Fill><sld:CssParameter name="fill">#AAAAAA</sld:CssParameter></sld:Fill></sld:PolygonSymbolizer>
</sld:Rule></sld:FeatureTypeStyle></sld:UserStyle>
</sld:NamedLayer></sld:StyledLayerDescriptor>"""


class Fake:
    def __init__(self):
        self.asked = []
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                fake.asked.append(q)
                if q.get("request") == "GetStyles":
                    body = GET_STYLES.encode()
                    self.send_response(200 if q.get("layers") == "geonode:rings" else 400)
                    self.send_header("Content-Type", "application/vnd.ogc.sld+xml")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                found = [dataset(fake.url, "rings")]
                if "filter{alternate}" in q and q["filter{alternate}"] != "geonode:rings":
                    found = []
                body = json.dumps({"total": len(found), "datasets": found}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.httpd.shutdown()


def test_published_datasets_are_the_products(fake):
    listing = GeoNode(url=f"{fake.url}/api/v2/").products("rings", 5)  # an API address is cut back to the GeoNode
    assert [(p.id, p.name, p.version, p.loads_as) for p in listing.products] == [
        ("geonode:rings", "Rings", "2026-10-22T10:00:00Z", "Shapefile")]
    assert fake.asked[-1] == {"filter{is_published}": "true", "page_size": "5", "search": "rings"}


def test_a_dataset_loads_its_best_export_never_the_original_upload(fake):
    p = GeoNode(url=fake.url).product("rings")  # without its workspace
    assert (p.chosen.format, p.chosen.file_name) == ("Shapefile", "rings.zip")
    assert "Original" not in [d.name for d in p.downloads]
    assert GeoNode(url=fake.url).product("geonode:rings", "GeoJSON").chosen.file_name == "rings.geojson"
    with pytest.raises(NotFound):
        GeoNode(url=fake.url).product("geonode:nothing")


def test_an_ogc_exception_report_is_said_as_the_servers_error():
    report = (b'<?xml version="1.0" ?><ServiceExceptionReport version="1.2.0"><ServiceException code="InvalidParameterValue" '
              b'locator="typeName">\n Feature type geonode:rutas_espana unknown</ServiceException></ServiceExceptionReport>')
    assert service_error(report) == "Feature type geonode:rutas_espana unknown"
    assert service_error(b"PK\x03\x04 a zip") is None


def test_a_datasets_styles_come_from_wms_getstyles(fake):
    client = GeoNode(url=fake.url)
    by_kind, default = client.styles(client.product("geonode:rings"))
    assert (by_kind.name, by_kind.default, by_kind.origin) == ("rings_by_kind", False, "GeoNode's style")
    assert (default.name, default.default) == ("rings", True)
    assert fake.asked[-1] == {"service": "WMS", "version": "1.1.1", "request": "GetStyles", "layers": "geonode:rings"}
    # each one an SLD of its own, for the layer by its name without the workspace
    assert by_kind.sld.count("<UserStyle>") == 1 and "<Name>rings</Name>" in by_kind.sld and "#ff0000" in by_kind.sld
    assert "#AAAAAA" not in by_kind.sld and "#AAAAAA" in default.sld
    assert "<ogc:PropertyName>Kind</ogc:PropertyName>" in by_kind.sld  # `dataops styles` writes it as the column: kind
