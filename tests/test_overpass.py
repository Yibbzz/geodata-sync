"""The overpass source type, against a fake Overpass API."""

import shutil

import pytest

from geoserver_dataops.loader import load
from geoserver_dataops.sources import SourceError
from geoserver_dataops.sources.overpass import Overpass, query
from geoserver_dataops.validate import validate
from fake_overpass import OverpassServer
from test_sync_integration import env, schemas, sync, tables_in  # noqa: F401 (fixtures)

needs_gdal = pytest.mark.skipif(shutil.which("ogr2ogr") is None, reason="GDAL's command-line tools not installed")
AREA = {"bbox": [-0.76, 52.65, -0.70, 52.69]}


@pytest.fixture
def server():
    s = OverpassServer()
    yield s
    s.close()


def test_a_selector_becomes_a_query_in_the_area():
    assert query("nwr[amenity=pub]; way[highway][name]", AREA["bbox"]) == (
        "[out:xml][timeout:270];(nwr[amenity=pub](52.65,-0.76,52.69,-0.7);way[highway][name](52.65,-0.76,52.69,-0.7););"
        "(._;>;);out body;")


@pytest.mark.parametrize("selector", ["pubs", "nwr[amenity=pub];out meta", "[out:json];node[a]", "node[a](1,2,3,4)", ""])
def test_only_element_types_and_tag_filters_are_a_selector(selector):
    with pytest.raises(SourceError, match="isn't an Overpass selector"):
        Overpass().product(selector)


def test_an_empty_area_is_refused():
    with pytest.raises(SourceError, match="is empty"):
        query("node[amenity]", [1, 1, 0, 2])


@needs_gdal
def test_the_features_are_fetched_with_only_the_layers_that_have_some(server, tmp_path):
    client = Overpass(url=server.url)
    p = client.selection("nwr[amenity=pub]", AREA)
    sha = client.fetch(p.chosen, tmp_path / "a.gpkg")
    assert server.user_agents[-1].startswith("geoserver-dataops/")  # Overpass refuses anonymous clients
    import subprocess
    layers = subprocess.run(["ogrinfo", "-ro", "-q", str(tmp_path / "a.gpkg")], capture_output=True, text=True).stdout
    assert "points" in layers and "multipolygons" in layers and "lines" not in layers
    # Overpass stamps every answer with its data's time: left out, the same features hash the same
    assert client.fetch(p.chosen, tmp_path / "b.gpkg") == sha
    server.extra = True
    assert client.fetch(p.chosen, tmp_path / "c.gpkg") != sha


def test_a_busy_overpass_says_so(server, tmp_path):
    server.busy = True
    client = Overpass(url=server.url)
    with pytest.raises(SourceError, match="busy"):
        client.fetch(client.selection("node[amenity]", AREA).chosen, tmp_path / "x.gpkg")


def test_validate_asks_an_overpass_dataset_for_its_area(tmp_path):
    (tmp_path / "p.yaml").write_text("""\
apiVersion: dataops/v1alpha1
kind: Project
metadata: { name: t }
spec: { database: { connection: { secret: db } } }
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: { name: osm }
spec: { type: overpass }
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: { name: pubs }
spec: { source: osm, product: "nwr[amenity=pub]" }
""")
    assert any("spec.overpass: required" in p.message for p in validate(load(tmp_path)))


@pytest.mark.integration
@needs_gdal
def test_pubs_load_and_again_only_when_they_change(tmp_path, server, schemas, env, capsys):
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
spec: {{ type: overpass, url: "{server.url}" }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec:
  source: osm
  product: "nwr[amenity=pub]"
  overpass: {{ bbox: [-0.76, 52.65, -0.70, 52.69] }}
""")
    assert sync(tmp_path / "p") == 0, capsys.readouterr().err
    assert tables_in(live) == ["sites_multipolygons", "sites_points"]
    assert sync(tmp_path / "p") == 0
    assert "sites: up to date (same file as last time)" in capsys.readouterr().out
    server.extra = True
    assert sync(tmp_path / "p") == 0
    assert "sites: loaded" in capsys.readouterr().out
