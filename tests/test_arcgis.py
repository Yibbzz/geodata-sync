"""The arcgis source type: ArcGIS REST services directories, against a fake ArcGIS server."""

import pytest

from geoserver_dataops import discover
from geoserver_dataops.cli import main
from geoserver_dataops.sources import NotFound, SourceError
from geoserver_dataops.sources.arcgis import ArcGIS, split
from geoserver_dataops.sources.formats import SERVICE_TYPES, service, service_address
from fake_arcgis import ArcGISServer


@pytest.fixture
def server():
    s = ArcGISServer()
    yield s
    s.close()


def test_any_address_inside_the_api_splits_into_directory_and_path():
    root = "https://services.arcgis.com/abc/arcgis/rest/services"
    assert split(root) == (root, "")
    assert split(f"{root}/Hydro/Water/FeatureServer/0/query?where=1%3D1") == (root, "Hydro/Water/FeatureServer/0")
    with pytest.raises(SourceError, match="no /rest/services"):
        split("https://example.org/data")


def test_a_ckan_link_to_an_arcgis_layer_says_how_to_discover_it():
    url = "https://x.example/arcgis/rest/services/Roads/FeatureServer/0/query?where=1=1&f=geojson"
    assert service("esri rest", url) == "ArcGIS REST" and SERVICE_TYPES["ArcGIS REST"] == "arcgis"
    assert service_address("ArcGIS REST", url) == "https://x.example/arcgis/rest/services/Roads/FeatureServer/0"
    for address in ("https://x.example/arcgis/rest/services/Roads/FeatureServer/0",
                    "https://x.example/arcgis/rest/services/Roads/FeatureServer/0/"):
        assert service_address("ArcGIS REST", address) == "https://x.example/arcgis/rest/services/Roads/FeatureServer/0"
    assert service_address("ArcGIS REST", "https://x.example/arcgis/rest/services/Roads/MapServer?f=json") == \
        "https://x.example/arcgis/rest/services/Roads/MapServer"


def test_the_directory_lists_feature_layers_and_tables(server):
    listing = ArcGIS(url=server.root).products()
    assert [p.id for p in listing.products] == [
        "Basemap/MapServer/0",  # a MapServer with no FeatureServer of the same name is listed
        "Hydro/Water/FeatureServer/0", "Hydro/Water/FeatureServer/1", "Hydro/Water/FeatureServer/3"]
    # The group layer (2), the geocoder, and Hydro/Water's MapServer twin are left out; Private refuses
    # without a token, which would stop the listing: a directory is read service by service
    assert listing.total == 4


def test_search_and_a_folder_in_the_address(server):
    assert [p.name for p in ArcGIS(url=f"{server.root}/Hydro").products("lakes").products] == ["Lakes"]


def test_a_layer_carries_its_last_edit_as_its_version(server):
    p = ArcGIS(url=server.root).product("Hydro/Water/FeatureServer/0")
    assert (p.name, p.version, p.description) == ("Rivers", "2025-10-09T08:53:20Z", "Main rivers.")
    assert p.chosen.service and p.chosen.url == f"{server.root}/Hydro/Water/FeatureServer/0"
    assert ArcGIS(url=server.root).product("Hydro/Water/FeatureServer/1").version is None  # reports no edits


def test_a_whole_address_works_as_the_product(server):
    assert ArcGIS(url=server.root).product(f"{server.root}/Hydro/Water/FeatureServer/0/query?f=json").id == \
        "Hydro/Water/FeatureServer/0"


@pytest.mark.parametrize("path, error, message", [
    ("Hydro/Water/FeatureServer/9", NotFound, None),
    ("Hydro/Water/FeatureServer/2", SourceError, "Group Layer"),
    ("Hydro/Water/FeatureServer", SourceError, "isn't a layer"),
    ("Private/FeatureServer/0", SourceError, "needs a token"),
])
def test_what_cant_be_loaded_says_why(server, path, error, message):
    with pytest.raises(error, match=message):
        ArcGIS(url=server.root).product(path)


def test_layers_name_the_table_geometry_crs_and_count(server):
    client = ArcGIS(url=server.root)
    rivers, lakes = (client.layers(client.product(f"Hydro/Water/FeatureServer/{i}"))[0] for i in (0, 1))
    assert (rivers.name, rivers.geometry, rivers.crs, rivers.features) == ("Rivers", "MultiLineString", "EPSG:27700", 25)
    assert (lakes.name, lakes.geometry, lakes.crs) == ("Lakes", "MultiPolygon", "EPSG:3857")  # Esri's 102100


def test_gdal_reads_the_layer_through_its_query_in_object_id_order(server):
    client = ArcGIS(url=server.root)
    connection, gdal_layer, name = client.gdal_source(client.product("Hydro/Water/FeatureServer/0"))
    assert connection == (f"ESRIJSON:{server.root}/Hydro/Water/FeatureServer/0/query"
                          "?where=1%3D1&outFields=*&returnGeometry=true&f=json&orderByFields=OBJECTID")
    assert (gdal_layer, name) == ("ESRIJSON", "Rivers")


def test_discover_shows_an_arcgis_layer(server, capsys, tmp_path):
    assert main(["discover", "arcgis", "Hydro/Water/FeatureServer/0", "--url", server.root, "-C", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Rivers (Hydro/Water/FeatureServer/0), version 2025-10-09" in out
    assert "Rivers  MultiLineString  EPSG:27700  25" in out
    discover.cache_path  # the version makes the layers cacheable, as a file's are
