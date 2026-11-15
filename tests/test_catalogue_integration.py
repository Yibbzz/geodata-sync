"""`dataops sync` of a catalogue record's file, through both catalogue protocols.
Against a real PostGIS (see test_sync_integration.py)."""

import pytest

from fake_catalogue import CatalogueServer
from test_sync_integration import env, query, schemas, sync  # noqa: F401 (fixtures)

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("kind, path", [("ogc-api-records", "/ogc"), ("csw", "/csw")])
def test_a_records_file_loads_and_reloads_only_when_the_record_changes(tmp_path, schemas, env, capsys, kind, path):
    live = schemas[0]
    server = CatalogueServer()
    try:
        (tmp_path / "project.yaml").write_text(f"""\
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
metadata: {{ name: catalogue }}
spec: {{ type: {kind}, url: "{server.base}{path}" }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec: {{ source: catalogue, product: flood-areas }}
""")
        assert sync(tmp_path) == 0, capsys.readouterr().err
        assert f"{live}.sites_flood (3 rows)" in capsys.readouterr().out
        assert query(f"SELECT count(*) FROM {live}.sites_flood") == [(3,)]

        assert sync(tmp_path) == 0
        assert "sites: up to date (2026-03-01)" in capsys.readouterr().out

        server.modified = "2026-04-01T00:00:00Z"
        server.points = {**server.points, "features": server.points["features"][:2]}
        assert sync(tmp_path) == 0
        assert f"{live}.sites_flood (2 rows)" in capsys.readouterr().out
    finally:
        server.close()
