"""`dataops sync` of a STAC selection: every matching item's file downloaded and mosaicked into one COG,
and nothing downloaded while the items stay the same. Against a real PostGIS (see test_sync_integration.py)."""

import json
import subprocess

import pytest

from fake_stac import STACServer, tile
from test_sync_integration import env, schemas, sync  # noqa: F401 (fixtures)

pytestmark = pytest.mark.integration


def project(tmp_path, url, schemas):
    (tmp_path / "project.yaml").write_text(f"""\
apiVersion: dataops/v1alpha1
kind: Project
metadata: {{ name: t }}
spec:
  database:
    connection: {{ secret: testdb }}
    schemas: {{ live: {schemas[0]}, staging: {schemas[1]}, backup: {schemas[2]} }}
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: {{ name: es }}
spec: {{ type: stac, url: "{url}" }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec:
  source: es
  product: dem
  stac: {{ bbox: [-1, 49, 5, 52], asset: data }}
  raster: {{ path: dem/heights.tif }}
""")
    return tmp_path


def stats(path):
    """Size, (min, max) and whether it is in EPSG:4326; worked out afresh each time (no .aux.xml kept)."""
    info = json.loads(subprocess.run(["gdalinfo", "-json", "-stats", "--config", "GDAL_PAM_ENABLED", "NO", str(path)],
                                     check=True, capture_output=True, text=True).stdout)
    band = info["bands"][0]
    return tuple(info["size"]), (band["minimum"], band["maximum"]), info["coordinateSystem"]["wkt"].count("4326") > 0


def test_a_stac_selection_becomes_one_cog_and_reloads_only_when_items_change(tmp_path, schemas, env, capsys, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("DATAOPS_DATA_DIR", str(data))
    (tmp_path / "tiles").mkdir()
    (tmp_path / "project").mkdir()
    server = STACServer(tmp_path / "tiles")
    try:
        root = project(tmp_path / "project", server.url, schemas)
        assert sync(root) == 0, capsys.readouterr().err
        out = capsys.readouterr().out
        assert "sites: loaded 3 items" in out and "dem/heights.tif (12 pixels)" in out
        assert stats(data / "dem/heights.tif") == ((6, 2), (10.0, 30.0), True)  # three tiles side by side
        files = lambda: sum(1 for r in server.requests if r[0] == "GET" and r[1].startswith("/files/"))  # noqa: E731
        assert files() == 3

        assert sync(root) == 0
        assert "sites: up to date (3 items" in capsys.readouterr().out
        assert files() == 3  # the version said so: no tile downloaded again

        # An item's metadata changes, its file doesn't: downloaded once to find that out, then not again
        server.items[2]["updated"] = "2026-06-01T00:00:00Z"
        assert sync(root) == 0
        assert "sites: up to date (same files as last time)" in capsys.readouterr().out and files() == 6
        assert sync(root) == 0
        assert "sites: up to date (3 items" in capsys.readouterr().out and files() == 6

        # A new file: loaded
        tile(tmp_path / "tiles", "tile2", 2.0, 99)
        server.items[2]["updated"] = "2026-07-01T00:00:00Z"
        assert sync(root) == 0
        assert "sites: loaded 3 items" in capsys.readouterr().out and files() == 9
        assert stats(data / "dem/heights.tif")[1] == (10.0, 99.0)
    finally:
        server.close()
