"""`geodata sync` from Socrata and OpenDataSoft portals: a dataset's GeoJSON export loaded, and not
exported again while its data is unchanged. Against a real PostGIS (see test_sync_integration.py)."""

import pytest

from fake_opendata import PortalServer
from test_sync_integration import env, schemas, sync  # noqa: F401 (fixtures)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind, product, table", [
    ("socrata", "trees-2015", "trees_2015"), ("socrata", "boro-bnds", "boro_bnds"), ("opendatasoft", "arbres", "arbres")])
def test_a_portal_dataset_loads_and_reloads_only_when_its_data_changes(tmp_path, schemas, env, capsys, kind, product, table):
    live = schemas[0]
    server = PortalServer()
    try:
        (tmp_path / "project.yaml").write_text(f"""\
apiVersion: geodata/v1alpha1
kind: Project
metadata: {{ name: t }}
spec:
  database:
    connection: {{ secret: testdb }}
    schemas: {{ live: {live}, staging: {schemas[1]}, backup: {schemas[2]} }}
---
apiVersion: geodata/v1alpha1
kind: Source
metadata: {{ name: portal }}
spec: {{ type: {kind}, url: "{server.url}" }}
---
apiVersion: geodata/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec: {{ source: portal, product: {product} }}
""")
        assert sync(tmp_path) == 0, capsys.readouterr().err
        assert f"{live}.sites_{table} (4 rows)" in capsys.readouterr().out  # GDAL names the layer after the file
        exports = lambda: sum(1 for path, _ in server.requests if "geojson" in path.lower())  # noqa: E731
        assert exports() == 1

        assert sync(tmp_path) == 0
        assert "sites: up to date (2026-02-01)" in capsys.readouterr().out and exports() == 1

        server.updated = "2026-05-01T00:00:00.000Z"
        server.points = {**server.points, "features": server.points["features"][:3]}
        assert sync(tmp_path) == 0
        assert f"{live}.sites_{table} (3 rows)" in capsys.readouterr().out
    finally:
        server.close()
