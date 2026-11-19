"""dataops validate: rules beyond the JSON Schema."""

from geoserver_dataops.loader import load
from geoserver_dataops.validate import validate

PROJECT = """\
apiVersion: dataops/v1alpha1
kind: Project
metadata: { name: t }
spec:
  database: { connection: { secret: pg } }
"""


def source(spec: str) -> str:
    return f"""---
apiVersion: dataops/v1alpha1
kind: Source
metadata: {{ name: s }}
spec: {spec}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: d }}
spec: {{ source: s, product: p }}
"""


def messages(tmp_path, spec):
    (tmp_path / "project.yaml").write_text(PROJECT + source(spec))
    return [(p.severity, p.message) for p in validate(load(tmp_path))]


def test_credentials_a_source_type_never_sends_are_a_warning(tmp_path):
    found = messages(tmp_path, "{ type: wfs, url: 'https://example.org/wfs', credentials: { secret: k } }")
    assert [m for m in found if "credentials" in m[1]] == [("warning", (
        "spec.credentials: a wfs source sends no credentials, so a service that needs them will refuse it; "
        "only arcgis, ckan, database, files, koordinates, opendatasoft, os-downloads, socrata, stac sources send credentials"))]


def test_credentials_where_they_are_sent_are_fine(tmp_path):
    for spec in ("{ type: os-downloads, credentials: { secret: k } }",
                 "{ type: ckan, url: 'https://data.example.org', credentials: { secret: k } }"):
        assert not [m for m in messages(tmp_path, spec) if "credentials" in m[1]]


def test_a_source_that_needs_a_key_says_so(tmp_path):
    (tmp_path / "project.yaml").write_text(PROJECT + source("{ type: koordinates, url: 'https://data.linz.govt.nz' }"))
    assert ("error", "spec.credentials: a koordinates source needs an API key: credentials: { secret: <name> }, with key apiKey") \
        in [(p.severity, p.message) for p in validate(load(tmp_path))]


def test_validate_takes_the_directory_with_c_or_alone(tmp_path, capsys):
    from geoserver_dataops.cli import main
    (tmp_path / "project.yaml").write_text(PROJECT + source("{ type: ckan, url: 'https://data.example.org' }"))
    for args in (["validate", "-C", str(tmp_path)], ["validate", "--dir", str(tmp_path)], ["validate", str(tmp_path)]):
        assert main(args) == 0, args
        assert "Valid: 3 resources in 1 files" in capsys.readouterr().out


def test_an_unquoted_date_is_text(tmp_path):
    # YAML reads 2025-08-22 as a date; the files mean the text, as if quoted
    (tmp_path / "project.yaml").write_text(PROJECT + """---
apiVersion: dataops/v1alpha1
kind: Source
metadata: { name: s }
spec: { type: stac, url: 'https://example.org/stac' }
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: { name: d }
spec:
  source: s
  product: sentinel-2-l2a
  stac: { bbox: [-74.06, 40.68, -73.90, 40.83], asset: visual, datetime: 2025-08-22 }
  raster: { path: rasters/s2.tif }
""")
    project = load(tmp_path)
    assert [r for r in project.resources if r.kind == "Dataset"][0].spec["stac"]["datetime"] == "2025-08-22"
    assert [p.message for p in validate(project) if p.severity == "error"] == []
