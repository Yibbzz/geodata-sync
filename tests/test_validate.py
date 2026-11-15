"""geodata validate: rules beyond the JSON Schema."""

from geodata_sync.loader import load
from geodata_sync.validate import validate

PROJECT = """\
apiVersion: geodata/v1alpha1
kind: Project
metadata: { name: t }
spec:
  database: { connection: { secret: pg } }
"""


def source(spec: str) -> str:
    return f"""---
apiVersion: geodata/v1alpha1
kind: Source
metadata: {{ name: s }}
spec: {spec}
---
apiVersion: geodata/v1alpha1
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
        "only ckan, opendatasoft, os-downloads, socrata sources use an API key"))]


def test_credentials_where_they_are_sent_are_fine(tmp_path):
    for spec in ("{ type: os-downloads, credentials: { secret: k } }",
                 "{ type: ckan, url: 'https://data.example.org', credentials: { secret: k } }"):
        assert not [m for m in messages(tmp_path, spec) if "credentials" in m[1]]
