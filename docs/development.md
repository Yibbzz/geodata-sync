# Development

## Tests

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                         # the PostGIS and GeoServer tests skip without one

docker run -d --rm --name test-postgis -p 55432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=gis postgis/postgis:18-3.7
TEST_DB_HOST=localhost TEST_DB_PORT=55432 .venv/bin/pytest -m integration
```

The e2e test ([`tests/test_e2e_geoserver.py`](../tests/test_e2e_geoserver.py)) has a real GeoServer publish what `geodata sync` loaded (a table and a Cloud-Optimised GeoTIFF) and serve it back through WFS and WCS, then loads new versions under the running GeoServer and checks it serves those:

```bash
tests/e2e/up.sh standard          # or no argument for GeoServer Cloud, standard-2 for GeoServer 2.28
E2E_GEOSERVER_URL=http://localhost:59091/geoserver/rest E2E_SERVICES_URL=http://localhost:59091/geoserver \
  .venv/bin/pytest -m e2e         # GeoServer Cloud: http://localhost:59090/geoserver-cloud[/rest]
tests/e2e/up.sh down
```

CI runs the tests with a real PostGIS (`test`), the e2e test on GeoServer Cloud and both standard GeoServers (`e2e-cloud`, `e2e-standard`, `e2e-standard-2`), shellcheck and a schema check of the Kubernetes example (`lint`), and the image build (`image`).

## New GeoServer versions

[`tests/e2e/up.sh`](../tests/e2e/up.sh) pins the versions the e2e test runs on, the same three GeoServer ConfigOps tests on, and the README names them. A daily job (`.github/workflows/versions.yaml`) runs `python tests/versions.py update`: when GeoServer Cloud or GeoServer has a new release with an image, it pins it, rewrites the README's badges and "Tested on" line, and opens a pull request, on which CI runs the e2e test. Merge it when it's green. The job needs Settings → Actions → General → "Allow GitHub Actions to create and approve pull requests".

`python tests/versions.py` shows what is pinned beside the newest releases, changing nothing.

## Releasing

Set the version in `pyproject.toml` and `geodata_sync/__init__.py`, merge, then push the tag `v<version>`: the Release workflow publishes the package to PyPI (`geodata-sync`), the image to `ghcr.io/yibbzz/geodata-sync`, and a GitHub release.
