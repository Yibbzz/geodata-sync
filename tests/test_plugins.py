"""Source types from other packages (docs/plugins.md): registered as an installed package registers them,
with an entry point in a .dist-info folder on the import path."""

import importlib
import json
import shutil
import sys
import textwrap

import pytest

from geoserver_dataops.cli import main
from geoserver_dataops.loader import load
from geoserver_dataops.sources import source_types
from geoserver_dataops.validate import validate
from test_sync_integration import env, schemas, sync, tables_in  # noqa: F401 (fixtures)

needs_gdal = pytest.mark.skipif(shutil.which("ogr2ogr") is None, reason="GDAL's command-line tools not installed")

PLUGIN = '''
import hashlib, json
from geoserver_dataops.sources import Download, Listing, NotFound, Product, SourceError, matches

SEEN = {}

class Parks:
    name = "parks API"
    formats = ["GeoJSON"]

    def __init__(self, api_key=None, url=None, session=None, credentials=None, options=None):
        SEEN.update(url=url, token=credentials("token") if credentials else None, options=options)

    def _product(self, name, rows=2):
        d = Download(format="GeoJSON", url="https://parks.example/" + name, file_name="parks.geojson", id=name, area=str(rows))
        return Product(id=name, name=name.title(), version=None, downloads=[d], loads_as="GeoJSON", chosen=d)

    def products(self, search=None, limit=None):
        return Listing([p for p in (self._product(n) for n in ("north", "south")) if matches(p, search)])

    def product(self, name, resource=None):
        if name not in ("north", "south"):
            raise NotFound(name)
        return self._product(name)

    def selection(self, name, options):
        SEEN["dataset_options"] = options
        return self._product(name, options.get("rows", 2))

    def download_url(self, download):
        return download.url

    def fetch(self, download, dest):
        body = json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"n": i}, "geometry": {"type": "Point", "coordinates": [i, 52]}}
            for i in range(int(download.area))]}).encode()
        dest.write_bytes(body)
        return hashlib.sha256(body).hexdigest()
'''


@pytest.fixture
def installed(tmp_path, monkeypatch):
    """Install `entries` ({type: "module:Class"}) as a package would, with the plugin module beside them."""
    def install(entries: dict[str, str], module: str = PLUGIN):
        site = tmp_path / "site"
        (site / "parks_plugin-1.0.dist-info").mkdir(parents=True)
        (site / "parks_plugin-1.0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: parks-plugin\nVersion: 1.0\n")
        (site / "parks_plugin-1.0.dist-info" / "entry_points.txt").write_text(
            "[geoserver_dataops.sources]\n" + "".join(f"{k} = {v}\n" for k, v in entries.items()))
        (site / "parks_plugin.py").write_text(textwrap.dedent(module))
        monkeypatch.syspath_prepend(str(site))
        importlib.invalidate_caches()
        sys.modules.pop("parks_plugin", None)
    return install


def project(tmp_path, source: str, dataset: str = "") -> object:
    (tmp_path / "p").mkdir(exist_ok=True)
    (tmp_path / "p" / "project.yaml").write_text(f"""\
apiVersion: dataops/v1alpha1
kind: Project
metadata: {{ name: t }}
spec: {{ database: {{ connection: {{ secret: testdb }} }} }}
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: {{ name: parks }}
spec: {source}
""" + (f"""---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec: {dataset}
""" if dataset else ""))
    return tmp_path / "p"


def test_an_installed_plugin_is_a_source_type(installed):
    installed({"parks": "parks_plugin:Parks"})
    assert source_types()["parks"].__name__ == "Parks"


def test_a_plugin_cant_replace_a_built_in_type(installed):
    installed({"wfs": "parks_plugin:Parks"})
    assert source_types()["wfs"].__name__ == "WFS"


def test_a_plugin_that_wont_load_is_left_out_with_a_note(installed, capsys):
    installed({"broken": "parks_plugin:Nothing", "parks": "parks_plugin:Parks"})
    assert "broken" not in source_types() and "parks" in source_types()
    assert "source type broken (from parks_plugin:Nothing) couldn't be loaded" in capsys.readouterr().err


def test_validate_knows_plugin_types_and_suggests_one_for_a_typo(installed, tmp_path):
    installed({"parks": "parks_plugin:Parks"})
    good = project(tmp_path, '{ type: parks, url: "https://parks.example", options: { area: north } }',
                   "{ source: parks, product: north, options: { rows: 3 } }")
    assert [p.message for p in validate(load(good)) if p.severity == "error"] == []
    (tmp_path / "typo").mkdir()
    bad = project(tmp_path / "typo", "{ type: park }")
    assert any("no source type 'park' (did you mean 'parks'?)" in p.message for p in validate(load(bad)))


def test_discover_gives_a_plugin_its_credentials_and_options(installed, tmp_path, capsys, monkeypatch):
    installed({"parks": "parks_plugin:Parks"})
    root = project(tmp_path, '{ type: parks, url: "https://parks.example", credentials: { secret: parks-api }, options: { area: north } }')
    monkeypatch.setenv("DATAOPS_PARKS_API_TOKEN", "t0k3n")
    assert main(["discover", "parks", "-C", str(root)]) == 0
    assert "north" in capsys.readouterr().out
    seen = sys.modules["parks_plugin"].SEEN
    assert (seen["url"], seen["token"], seen["options"]) == ("https://parks.example", "t0k3n", {"area": "north"})


@pytest.mark.integration
@needs_gdal
def test_a_plugins_dataset_loads_with_its_options(installed, tmp_path, schemas, env, capsys):
    installed({"parks": "parks_plugin:Parks"})
    live = schemas[0]
    root = project(tmp_path, "{ type: parks }", "{ source: parks, product: north, options: { rows: 3 } }")
    text = (root / "project.yaml").read_text().replace(
        "spec: { database: { connection: { secret: testdb } } }",
        f"spec: {{ database: {{ connection: {{ secret: testdb }}, schemas: {{ live: {live}, staging: {schemas[1]}, backup: {schemas[2]} }} }} }}")
    (root / "project.yaml").write_text(text)
    assert sync(root) == 0, capsys.readouterr().err
    assert tables_in(live) == ["sites_parks"] and "(3 rows)" in capsys.readouterr().out
    assert sys.modules["parks_plugin"].SEEN["dataset_options"] == {"rows": 3}
    assert sync(root) == 0
    assert "sites: up to date (same file as last time)" in capsys.readouterr().out
