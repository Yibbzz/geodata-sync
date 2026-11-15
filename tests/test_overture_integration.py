"""`geodata sync` of an Overture type in an area, from the fake bucket, into a real PostGIS
(see test_sync_integration.py)."""

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("shapely")

from fake_overture import OvertureBucket  # noqa: E402
from test_sync_integration import env, query, schemas, sync  # noqa: E402, F401 (fixtures)

pytestmark = pytest.mark.integration


def test_an_overture_area_loads_with_its_structures_as_json(tmp_path, schemas, env, capsys):
    live = schemas[0]
    bucket = OvertureBucket(tmp_path)
    try:
        (tmp_path / "p").mkdir()
        (tmp_path / "p" / "project.yaml").write_text(f"""\
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
metadata: {{ name: overture }}
spec: {{ type: overture, url: "{bucket.url}" }}
---
apiVersion: geodata/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec: {{ source: overture, product: places/place, overture: {{ bbox: [0, 51, 1, 52] }} }}
""")
        assert sync(tmp_path / "p") == 0, capsys.readouterr().err
        assert f"{live}.sites_place (3 rows)" in capsys.readouterr().out
        assert query(f"SELECT names::json->>'primary', categories, ST_SRID(geom) FROM {live}.sites_place WHERE id = 'a'") == \
            [("place a", ["cafe"], 4326)]
        assert sync(tmp_path / "p") == 0
        assert "sites: up to date (2026-10-21.0 (" in capsys.readouterr().out
    finally:
        bucket.close()
