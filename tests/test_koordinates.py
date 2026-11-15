"""The koordinates source type, against a fake Koordinates portal."""

import shutil

import pytest

import geoserver_dataops.sources.koordinates as koordinates
from geoserver_dataops.sources import NotFound, SourceError
from geoserver_dataops.sources.koordinates import Koordinates
from fake_koordinates import KoordinatesPortal, gpkg_zip
from test_sync_integration import env, query, schemas, sync  # noqa: F401 (fixtures)

pytestmark = pytest.mark.skipif(shutil.which("ogr2ogr") is None, reason="GDAL's command-line tools not installed")


@pytest.fixture
def portal(tmp_path, monkeypatch):
    monkeypatch.setattr(koordinates, "POLL", 0)
    p = KoordinatesPortal(tmp_path)
    yield p
    p.close()


def test_a_key_is_needed(portal):
    with pytest.raises(SourceError, match="needs an API key"):
        Koordinates(url=portal.url)


def test_layers_are_found_by_search_with_the_key(portal):
    client = Koordinates(api_key="s3cr3t", url=f"{portal.url}/layer/50772-nz-primary-parcels/")
    assert [(p.id, p.name) for p in client.products("parcels").products] == [("50772", "NZ Primary Parcels")]
    p = client.product("50772")
    assert (p.version, p.chosen.area) == ("1001", "EPSG:2193")
    assert not portal.unauthorised


def test_rasters_and_unknown_layers_say_so(portal):
    client = Koordinates(api_key="s3cr3t", url=portal.url)
    with pytest.raises(SourceError, match="is a raster"):
        client.product("51000")
    with pytest.raises(NotFound):
        client.product("1")
    with pytest.raises(SourceError, match="check the API key"):
        Koordinates(api_key="wrong", url=portal.url).product("50772")


def test_an_export_is_asked_for_waited_on_and_downloaded_without_the_key(portal, tmp_path):
    client = Koordinates(api_key="s3cr3t", url=portal.url)
    p = client.product("50772")
    client.fetch(p.chosen, tmp_path / "layer.zip")
    export = portal.exports[0]
    assert export["body"] == {"items": [{"item": f"{portal.url}/services/api/v1.x/layers/50772/"}],
                              "formats": {"vector": "application/x-ogc-gpkg", "table": "application/x-ogc-gpkg"},
                              "crs": "EPSG:2193"}
    assert export["polls"] == 1
    assert portal.storage_auth == [None]  # the key went to the portal, not to the storage it redirected to
    assert (tmp_path / "layer.zip").read_bytes() == portal.file


@pytest.mark.integration
def test_a_layer_loads_and_isnt_exported_again_until_a_new_version(portal, tmp_path, schemas, env, capsys, monkeypatch):
    live = schemas[0]
    monkeypatch.setenv("DATAOPS_LINZ_APIKEY", "s3cr3t")
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
metadata: {{ name: linz }}
spec: {{ type: koordinates, url: "{portal.url}", credentials: {{ secret: linz }} }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec: {{ source: linz, product: "50772" }}
""")
    assert sync(tmp_path / "p") == 0, capsys.readouterr().err
    assert f"{live}.sites_nz_parcels (3 rows)" in capsys.readouterr().out
    assert query(f"SELECT min(ST_SRID(geom)) FROM {live}.sites_nz_parcels") == [(2193,)]
    assert sync(tmp_path / "p") == 0
    assert "sites: up to date (1001)" in capsys.readouterr().out and len(portal.exports) == 1

    portal.version, portal.file = 1002, gpkg_zip(tmp_path / "v2", "abcd")
    assert sync(tmp_path / "p") == 0
    assert f"{live}.sites_nz_parcels (4 rows)" in capsys.readouterr().out and len(portal.exports) == 2
