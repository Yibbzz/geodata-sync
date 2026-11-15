"""The ogc-api-records and csw source types, against a fake catalogue that speaks both."""

import pytest

from geodata_sync.cli import main
from geodata_sync.sources import NotFound, SourceError
from geodata_sync.sources.catalogue import CSW, OGCAPIRecords
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
    with pytest.raises(SourceError, match="not a file geodata can load"):
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
    assert "discover it: geodata discover arcgis --url https://gis.example/arcgis/rest/services/Parcels/FeatureServer/0" in out
