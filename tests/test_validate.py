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
        "only ckan, database, files, koordinates, opendatasoft, os-downloads, socrata sources send credentials"))]


def test_credentials_where_they_are_sent_are_fine(tmp_path):
    for spec in ("{ type: os-downloads, credentials: { secret: k } }",
                 "{ type: ckan, url: 'https://data.example.org', credentials: { secret: k } }"):
        assert not [m for m in messages(tmp_path, spec) if "credentials" in m[1]]


def test_a_source_that_needs_a_key_says_so(tmp_path):
    (tmp_path / "project.yaml").write_text(PROJECT + source("{ type: koordinates, url: 'https://data.linz.govt.nz' }"))
    assert ("error", "spec.credentials: a koordinates source needs an API key: credentials: { secret: <name> }, with key apiKey") \
        in [(p.severity, p.message) for p in validate(load(tmp_path))]
