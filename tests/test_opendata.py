"""The socrata and opendatasoft source types, against fake portals."""

import pytest

from geodata_sync.sources import NotFound, SourceError
from geodata_sync.sources.opendata import OpenDataSoft, Socrata
from fake_opendata import PortalServer


@pytest.fixture
def server():
    s = PortalServer()
    yield s
    s.close()


def test_socrata_lists_only_datasets_with_geometry(server):
    listing = Socrata(url=server.url).products()
    assert [p.id for p in listing.products] == ["trees-2015", "boro-bnds"]  # 311 calls has no geometry column
    assert [p.id for p in Socrata(url=server.url).products("trees").products] == ["trees-2015"]


def test_socrata_exports_a_table_and_a_map_as_geojson(server):
    client = Socrata(url=f"{server.url}/d/trees-2015")  # any page of the portal names it
    trees, boroughs = client.product("trees-2015"), client.product("boro-bnds")
    assert trees.version == "2026-02-01T00:00:00.000Z"  # when its data changed, not its description
    assert trees.chosen.url == f"{server.url}/resource/trees-2015.geojson?$limit=1000000000&$order=:id"
    assert boroughs.chosen.url == f"{server.url}/api/geospatial/boro-bnds?method=export&format=GeoJSON"
    assert trees.chosen.file_name == "trees-2015.geojson"


def test_socrata_says_why_a_dataset_cant_be_loaded(server):
    with pytest.raises(SourceError, match="no geometry column"):
        Socrata(url=server.url).product("311-calls")
    with pytest.raises(NotFound):
        Socrata(url=server.url).product("nope-nope")


def test_socrata_sends_an_app_token_to_the_portal(server):
    Socrata(api_key="tok123", url=server.url).products()
    assert server.requests[-1][1].get("X-App-Token") == "tok123"


def test_opendatasoft_lists_and_exports_datasets_with_geometry(server):
    client = OpenDataSoft(url=server.url)
    assert [p.id for p in client.products().products] == ["arbres"]
    assert client.products("trees").total == 0
    p = client.product("arbres")
    assert (p.name, p.version) == ("Arbres", "2026-02-01T00:00:00.000Z")
    assert p.chosen.url == f"{server.url}/api/explore/v2.1/catalog/datasets/arbres/exports/geojson"
    with pytest.raises(SourceError, match="no geometry"):
        client.product("budget")
    with pytest.raises(NotFound):
        client.product("nope")
