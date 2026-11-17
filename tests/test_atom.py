"""The atom source type (INSPIRE Atom download services), against a fake service."""

import shutil

import pytest

from geoserver_dataops.cli import main
from geoserver_dataops.sources import NotFound, SourceError
from geoserver_dataops.sources.atom import Atom, short_id
from fake_atom import AtomServer, geopackage
from test_sync_integration import env, schemas, sync, tables_in  # noqa: F401 (fixtures)

needs_gdal = pytest.mark.skipif(shutil.which("ogr2ogr") is None, reason="GDAL's command-line tools not installed")


@pytest.fixture
def server(tmp_path):
    s = AtomServer(tmp_path)
    yield s
    s.close()


@needs_gdal
def test_the_service_feeds_datasets_are_the_products(server):
    listing = Atom(url=f"{server.url}/atom/index.xml").products()
    assert [(p.id, p.name, p.version) for p in listing.products] == [
        ("protected_sites", "Protected sites", "2026-06-03T12:00:00Z"), ("maps_only", "Scanned maps", "2020-01-01T00:00:00Z")]
    assert [p.id for p in Atom(url=f"{server.url}/atom/index.xml").products("reserves").products] == ["protected_sites"]


@needs_gdal
def test_a_datasets_files_come_from_its_own_feed_and_the_best_is_chosen(server):
    p = Atom(url=f"{server.url}/atom/index.xml").product("protected_sites")
    assert [(d.format, d.subformat, d.size) for d in p.downloads] == [("GML", "ETRS89", 900), ("GeoPackage", "WGS 84", 98304)]
    assert p.chosen.url.endswith("/downloads/sites.gpkg") and p.loads_as == "GeoPackage"
    gml = Atom(url=f"{server.url}/atom/index.xml").product("protected_sites", f"{server.url}/downloads/sites.gml")
    assert gml.chosen.format == "GML"
    assert not server.downloads()  # nothing downloaded to find that out


@needs_gdal
def test_what_cant_be_loaded_says_why(server):
    client = Atom(url=f"{server.url}/atom/index.xml")
    with pytest.raises(NotFound):
        client.product("atlantis")
    assert client.product("maps_only").chosen is None  # a PNG: nothing dataops loads
    with pytest.raises(SourceError, match="Atom feed"):
        Atom(url=f"{server.url}/downloads/sites.gpkg").products()
    with pytest.raises(SourceError, match="needs the download service's feed"):
        Atom()


def test_a_datasets_id_is_its_feeds_file_name():
    assert short_id("https://service.pdok.nl/rvo/natura2000/atom/natura_2000.xml") == "natura_2000"
    assert short_id("https://geo.example/atom/964fe1c4-f708.xml") == "964fe1c4-f708"


@needs_gdal
def test_discover_lists_the_datasets(server, capsys, tmp_path):
    assert main(["discover", "atom", "--url", f"{server.url}/atom/index.xml", "-C", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "protected_sites" in out and "GeoPackage" in out and "2026-06-03" in out


@pytest.mark.integration
@needs_gdal
def test_a_dataset_loads_and_again_only_when_its_feed_changes(tmp_path, server, schemas, env, capsys):
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
metadata: {{ name: inspire }}
spec: {{ type: atom, url: "{server.url}/atom/index.xml" }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec: {{ source: inspire, product: protected_sites }}
""")
    assert sync(tmp_path / "p") == 0, capsys.readouterr().err
    assert tables_in(live) == ["sites_protected_sites"] and len(server.downloads()) == 1
    assert sync(tmp_path / "p") == 0
    assert "sites: up to date (2026-06-03)" in capsys.readouterr().out and len(server.downloads()) == 1

    server.updated = "2026-07-01T00:00:00Z"  # a new version, with new data
    server.files["/downloads/sites.gpkg"] = geopackage(tmp_path / "again", sites=3)
    assert sync(tmp_path / "p") == 0
    assert "sites: loaded 2026-07-01" in capsys.readouterr().out and len(server.downloads()) == 2
