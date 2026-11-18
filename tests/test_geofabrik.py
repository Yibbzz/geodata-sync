"""The geofabrik source type, against a fake Geofabrik."""

import shutil

import pytest

from geoserver_dataops.sources import NotFound, SourceError
from geoserver_dataops.sources.geofabrik import Geofabrik
from fake_geofabrik import GeofabrikServer, shapefile_pack
from test_sync_integration import env, schemas, sync, tables_in  # noqa: F401 (fixtures)

needs_gdal = pytest.mark.skipif(shutil.which("ogr2ogr") is None, reason="GDAL's command-line tools not installed")


@pytest.fixture
def server(tmp_path):
    s = GeofabrikServer(tmp_path)
    yield s
    s.close()


@needs_gdal
def test_regions_are_the_products(server):
    listing = Geofabrik(url=server.url).products()
    assert [p.id for p in listing.products] == ["big", "europe", "test"]
    assert [p.id for p in Geofabrik(url=server.url).products("testshire").products] == ["test"]


@needs_gdal
def test_the_shapefiles_where_there_are_some_else_the_osm_extract(server):
    client = Geofabrik(url=server.url)
    test, big = client.product("test"), client.product("big")
    assert (test.chosen.format, test.chosen.file_name) == ("Shapefile", "test-latest-free.shp.zip")
    assert (big.chosen.format, big.chosen.file_name) == ("OSM PBF", "big-latest.osm.pbf")
    assert test.version == "2025-10-09T08:53:20Z"  # when Geofabrik built it, from one byte
    assert len(test.chosen.md5) == 32
    assert client.product("test", "pbf").chosen.format == "OSM PBF"
    assert not server.downloads()  # nothing downloaded to find all that out


@needs_gdal
def test_what_cant_be_loaded_says_why(server):
    client = Geofabrik(url=server.url)
    with pytest.raises(NotFound):
        client.product("atlantis")
    with pytest.raises(SourceError, match="no extract of europe"):
        client.product("europe")
    with pytest.raises(SourceError, match="pick shp"):
        client.product("test", "gpkg")


@pytest.mark.integration
@needs_gdal
@pytest.mark.parametrize("region, resource, tables", [
    ("test", None, ["sites_gis_osm_pois_free_1", "sites_gis_osm_roads_free_1"]),
    ("big", None, ["sites_lines", "sites_points"]),
])
def test_an_extract_loads_and_reloads_only_once_rebuilt(tmp_path, server, schemas, env, capsys, region, resource, tables):
    live = schemas[0]
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "project.yaml").write_text(f"""\
apiVersion: dataops/v1alpha1
kind: Project
metadata: {{ name: t }}
spec:
  database:
    connection: {{ secret: testdb }}
    schemas: {{ live: {live}, staging: {schemas[1]}, backup: {schemas[2]} }}
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: {{ name: osm }}
spec: {{ type: geofabrik, url: "{server.url}" }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec:
  source: osm
  product: {region}
  tables: {{ {", ".join(t.removeprefix("sites_") + ": {}" for t in tables)} }}
""")
    assert sync(tmp_path / "p") == 0, capsys.readouterr().err
    assert tables_in(live) == tables
    assert len(server.downloads()) == 1

    assert sync(tmp_path / "p") == 0
    assert "sites: up to date" in capsys.readouterr().out and len(server.downloads()) == 1

    server.built += 86_400  # rebuilt the next day, with nothing new in it: downloaded to find out, not loaded
    assert sync(tmp_path / "p") == 0
    assert "sites: up to date (same file as last time)" in capsys.readouterr().out and len(server.downloads()) == 2

    server.built += 86_400  # and the day after, with new data
    for path in server.files:
        server.files[path] += b" " if path.endswith(".pbf") else b""
    server.files["/europe/test-latest-free.shp.zip"] = shapefile_pack(tmp_path / "again", cafes=3) if region == "test" else \
        server.files["/europe/test-latest-free.shp.zip"]
    assert sync(tmp_path / "p") == 0
    assert "sites: loaded" in capsys.readouterr().out and len(server.downloads()) == 3
