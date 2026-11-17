"""The ogc-api-records and csw source types, against a fake catalogue that speaks both."""

import xml.etree.ElementTree as ET

import pytest

from geoserver_dataops.cli import main
from geoserver_dataops.sources import NotFound, SourceError
from geoserver_dataops.sources.catalogue import CSW, NS, OGCAPIRecords, csw_product, when
from fake_catalogue import CatalogueServer


@pytest.fixture
def server():
    s = CatalogueServer()
    yield s
    s.close()


@pytest.fixture(params=["ogc-api-records", "csw"])
def client(request, server):
    return OGCAPIRecords(url=f"{server.base}/ogc") if request.param == "ogc-api-records" else CSW(url=f"{server.base}/csw")


def test_records_are_the_products(client):
    listing = client.products()
    assert [(p.id, p.name) for p in listing.products] == [("flood-areas", "Flood areas"), ("parcels", "Land parcels")]
    assert listing.total == 2
    assert [p.id for p in client.products("cadastral").products] == ["parcels"]


def test_a_records_file_is_chosen_and_its_services_listed(client, server):
    p = client.product("flood-areas")
    assert p.version == "2026-03-01T00:00:00Z" and p.description == "Areas at risk of flooding."
    assert (p.chosen.format, p.chosen.url) == ("GeoJSON", f"{server.base}/files/flood.geojson")
    kinds = {d.name: (d.format, d.loadable) for d in p.downloads}
    assert kinds["Flood areas WFS"] == ("WFS", False)


def test_a_record_with_only_a_service_points_to_it(client):
    p = client.product("parcels")
    assert p.chosen is None and [(d.format, d.loadable) for d in p.downloads] == [("ArcGIS REST", False)]


def test_resource_picks_a_link_and_refuses_one_that_isnt_a_file(client):
    assert client.product("flood-areas", "GeoJSON").chosen.name == "GeoJSON"
    with pytest.raises(SourceError, match="not a file dataops can load"):
        client.product("flood-areas", "Flood areas WFS")
    with pytest.raises(SourceError, match="has no link 'nope'"):
        client.product("flood-areas", "nope")


def test_an_unknown_record(client):
    with pytest.raises(NotFound):
        client.product("nope")


def test_a_catalogues_own_address_and_its_first_catalogue(server):
    assert OGCAPIRecords(url=f"{server.base}/ogc").catalogue() == "main"  # the collection of records, not of features
    assert OGCAPIRecords(url=f"{server.base}/ogc/collections/main/items?q=x").catalogue() == "main"


def test_a_csw_error_is_reported(server):
    with pytest.raises(SourceError, match="Operation not supported"):
        CSW(url=f"{server.base}/csw")._xml({"request": "Harvest"}, "x")


def test_discover_suggests_how_to_read_a_records_service(server, capsys, tmp_path):
    assert main(["discover", "csw", "parcels", "--url", f"{server.base}/csw", "-C", str(tmp_path), "--no-layers"]) == 0
    out = capsys.readouterr().out
    assert "discover it: dataops discover arcgis --url https://gis.example/arcgis/rest/services/Parcels/FeatureServer/0" in out


def test_a_time_in_milliseconds_is_a_date():
    """ArcGIS Hub's records give `modified` as milliseconds since 1970; discover printed it with len() and failed."""
    assert when(1794926649000) == "2026-11-17T14:44:09Z"
    assert when(1794926649) == "2026-11-17T14:44:09Z"
    assert when("2026-11-04") == "2026-11-04"
    assert when(None) is None and when("") is None


def test_a_record_in_milliseconds_is_listed(server, monkeypatch, capsys, tmp_path):
    real = OGCAPIRecords._product
    monkeypatch.setattr(OGCAPIRecords, "_product", lambda self, r: real(self, {**r, "properties": {
        **{k: v for k, v in (r.get("properties") or {}).items() if k not in ("updated", "modified", "created")},
        "modified": 1794926649000}}))
    assert main(["discover", "ogc-api-records", "--url", f"{server.base}/ogc", "-C", str(tmp_path)]) == 0
    assert "2026-11-17" in capsys.readouterr().out


def test_a_geonetwork_title_in_several_languages_is_one_line():
    """GeoNetwork writes each language of a title on its own line of dc:title, often the same words again."""
    record = ET.fromstring(
        f'<csw:Record xmlns:csw="{NS["csw"]}" xmlns:dc="{NS["dc"]}"><dc:identifier>r1</dc:identifier>'
        "<dc:title>Forstreviere\r\n   \r\n      Forstreviere\n  Forest districts  </dc:title></csw:Record>")
    assert csw_product(record).name == "Forstreviere / Forest districts"


def test_an_arcgis_hub_record_is_its_item(server, monkeypatch):
    """ArcGIS Hub's records link to nothing: a file item is downloaded from ArcGIS Online by its ID, and a
    service item's `url` is listed as the service to discover."""
    hub = {"id": "8da5c3ebcc014486a61709ca8e6bfdbb", "links": [{"rel": "self", "href": "https://hub.example/x"}],
           "properties": {"title": "Parks", "type": "GeoJson", "modified": 1794926649000, "url": None}}
    p = OGCAPIRecords(url=f"{server.base}/ogc")._product(hub)
    assert p.version == "2026-11-17T14:44:09Z"
    [d] = [d for d in p.downloads if d.loadable]
    assert (d.format, d.file_name) == ("GeoJSON", "8da5c3ebcc014486a61709ca8e6bfdbb.geojson")
    assert d.url == "https://www.arcgis.com/sharing/rest/content/items/8da5c3ebcc014486a61709ca8e6bfdbb/data"
    service = {"id": "ff8b2bcabffa4982861ef8e97bed7645_7", "properties": {
        "title": "Parks", "type": "Feature Service", "url": "https://services1.arcgis.com/o/arcgis/rest/services/Parks/FeatureServer"}}
    p = OGCAPIRecords(url=f"{server.base}/ogc")._product(service)
    assert [(d.format, d.loadable) for d in p.downloads] == [("ArcGIS REST", False)]
