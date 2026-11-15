"""`geodata sync` of an ArcGIS REST layer into a real PostGIS, from the fake ArcGIS server: GDAL pages
through it, the table is named after the layer, and its last edit is the version.
Against a real PostGIS (see test_sync_integration.py)."""

import pytest

from fake_arcgis import ArcGISServer
from test_sync_integration import env, query, schemas, sync, tables_in  # noqa: F401 (fixtures)

pytestmark = pytest.mark.integration


def project(tmp_path, root, schemas, tables=""):
    (tmp_path / "project.yaml").write_text(f"""\
apiVersion: geodata/v1alpha1
kind: Project
metadata: {{ name: t }}
spec:
  database:
    connection: {{ secret: testdb }}
    schemas: {{ live: {schemas[0]}, staging: {schemas[1]}, backup: {schemas[2]} }}
---
apiVersion: geodata/v1alpha1
kind: Source
metadata: {{ name: hydro }}
spec: {{ type: arcgis, url: "{root}" }}
---
apiVersion: geodata/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec:
  source: hydro
  product: Hydro/Water/FeatureServer/0
{tables}""")
    return tmp_path


def test_an_arcgis_layer_loads_and_reloads_only_when_edited(tmp_path, schemas, env, capsys):
    live = schemas[0]
    server = ArcGISServer(rivers=25)
    try:
        root = project(tmp_path, server.root, schemas)
        assert sync(root) == 0, capsys.readouterr().err
        assert f"{live}.sites_rivers (25 rows)" in capsys.readouterr().out  # 3 pages of 10
        assert query(f"SELECT count(DISTINCT name), min(ST_SRID(geom)) FROM {live}.sites_rivers") == [(25, 27700)]
        pages = [r for r in server.requests if "/query?" in r and "resultOffset=" in r]
        assert pages and all("orderByFields=OBJECTID" in r for r in pages)

        assert sync(root) == 0
        assert "sites: up to date (2025-10-09)" in capsys.readouterr().out  # same last edit: not loaded again

        server.rivers, server.edited = 27, server.edited + 60_000
        assert sync(root) == 0
        assert f"{live}.sites_rivers (27 rows)" in capsys.readouterr().out
    finally:
        server.close()


def test_tables_names_an_arcgis_layer_by_its_name(tmp_path, schemas, env, capsys):
    server = ArcGISServer(rivers=3)
    try:
        root = project(tmp_path, server.root, schemas, tables="  tables: { Rivers: { name: rivers } }\n")
        assert sync(root) == 0, capsys.readouterr().err
        assert tables_in(schemas[0]) == ["rivers"]
    finally:
        server.close()
