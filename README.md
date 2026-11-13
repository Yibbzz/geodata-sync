# geodata-sync

Open GIS data loaded into PostGIS, and kept up to date, from YAML files. Say which datasets you want, where they come from and how often to check them; `geodata sync` downloads each new version, loads it into staging tables, checks it, backs up the old tables and swaps the new ones in, in one transaction.

- **Find data:** the Ordnance Survey's OpenData downloads, CKAN portals (data.gov.uk and many city, regional and national ones), WFS and OGC API Features services, or plain links. `geodata discover` lists what a source offers and the layers inside a download.
- **Load it safely:** staging, invalid geometries left out, row counts checked, the old tables kept as backups; a load that fails changes nothing.
- **Keep it up to date:** each dataset has a schedule (`daily`, `weekly`, `monthly` or a cron expression); `geodata sync` run nightly checks those that are due, and loads only a new version.
- **Rasters too:** a raster download (OS Terrain 50's 2,800 grid tiles, say) becomes one Cloud-Optimised GeoTIFF.

It only loads data. To publish the tables in GeoServer or GeoServer Cloud, use [GeoServer ConfigOps](https://github.com/Yibbzz/geoserver-configops): `configops refresh` brings GeoServer up to date after a load.

> **Status: early.** Expect breaking changes.

## Install

```bash
pip install geodata-sync                               # the geodata command; it needs GDAL's tools (apt install gdal-bin)
docker run --rm ghcr.io/yibbzz/geodata-sync --help     # or its image, GDAL included
```

## Quick start

With `docker` and `geodata`. This starts a PostGIS database ([`examples/compose.yaml`](examples/compose.yaml)) and loads [`examples/quickstart`](examples/quickstart/project.yaml): Surrey Heath's conservation areas, from data.gov.uk.

```yaml
apiVersion: geodata/v1alpha1
kind: Project
metadata: { name: quickstart }
spec:
  database:
    connection: { secret: database }     # keys: host, port, database, username, password
  crs: EPSG:27700
---
apiVersion: geodata/v1alpha1
kind: Source
metadata: { name: data-gov-uk }
spec: { type: ckan, url: https://data.gov.uk }
---
apiVersion: geodata/v1alpha1
kind: Dataset
metadata: { name: conservation-areas }
spec:
  source: data-gov-uk
  product: conservation-areas125         # `geodata discover data-gov-uk --search "conservation areas"` finds it
  update: monthly
  tables:
    conservation_areas: {}               # the layer in the download; table conservation_areas_conservation_areas
```

```bash
docker compose -f examples/compose.yaml up -d --wait

cat > examples/quickstart/secrets.yaml <<'EOF'          # git-ignored
database: { host: localhost, port: "5433", database: postgres, username: postgres, password: postgres }
EOF

geodata sync -C examples/quickstart
#   conservation-areas: loaded 2026-11-11 in 0s: data_layers.conservation_areas_conservation_areas (10 rows)
#     note: 1 invalid geometry left out
geodata status -C examples/quickstart
#   DATASET             UPDATE   LOADED      AT                ROWS  NEXT CHECK        LAST CHECK
#   conservation-areas  monthly  2026-11-11  2026-11-13 08:51  10    2026-12-13 07:51  success
geodata sync -C examples/quickstart
#   conservation-areas: not due (monthly)
```

`docker compose -f examples/compose.yaml down -v` removes it all. [docs/spec.md](docs/spec.md) describes every kind, field and command.

## Examples

| Project | What it holds |
| --- | --- |
| [`examples/quickstart`](examples/quickstart/) | The quick start above: one dataset from data.gov.uk, 10 polygons. Loads in seconds |
| [`examples/uk-basemap`](examples/uk-basemap/) | OS Open Roads (7.3M features), OS Boundary-Line, the Environment Agency's flood zones (415k polygons) and the conservation areas. Downloads about 3 GB; the first load takes 20 minutes or so. GeoServer ConfigOps's `examples/uk-basemap` publishes them |
| [`examples/terrain`](examples/terrain/) | OS Terrain 50 (heights of Great Britain, 50 m) made into one Cloud-Optimised GeoTIFF |

## How to

**Find a dataset:**

```bash
geodata discover os-downloads                                   # OS products
geodata discover data-gov-uk --search "flood zones" -C my-data  # a Source in the project
geodata discover data-gov-uk <product> -C my-data               # its files and the layers inside
geodata validate my-data                                        # problems come out as file:line
```

**Load now** rather than when due: `geodata sync -C my-data <dataset>` (`--force` loads it even when the version hasn't changed).

**Refresh GeoServer after a load:** `geodata sync -C my-data --loaded loaded.txt` writes what it loaded (`schema.table`, or a raster's path), and `configops refresh -C my-geoserver --from loaded.txt` works out the bounds of the layers on them again and clears their cached tiles.

**Keep the logins out of the files.** A project names Secrets (`connection: { secret: database }`); `geodata` reads each from a git-ignored `secrets.yaml` in the project folder, from `GEODATA_<SECRET>_<KEY>` environment variables, or from files `$GEODATA_SECRETS_DIR/<secret>/<key>` (how Kubernetes mounts Secrets).

**Run it in Kubernetes:** [docs/kubernetes.md](docs/kubernetes.md) has a nightly CronJob that loads, then refreshes GeoServer.

[docs/sources-notes.md](docs/sources-notes.md) has what we learned about the sources: big layers over WFS or OGC API Features don't load reliably, so use a file download when there is one.

## Testing

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                         # the PostGIS tests skip without one

docker run -d --rm --name test-postgis -p 55432:5432 \
  -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=gis postgis/postgis:18-3.7
TEST_DB_HOST=localhost TEST_DB_PORT=55432 .venv/bin/pytest -m integration
```

CI runs the tests with a real PostGIS (test), shellcheck and a schema check of the Kubernetes example (lint), and the image build (image).

## Releasing

Set the version in `pyproject.toml` and `geodata_sync/__init__.py`, merge, then push the tag `v<version>`: the Release workflow publishes the package to PyPI (`geodata-sync`), the image to `ghcr.io/yibbzz/geodata-sync`, and a GitHub release.

## Licence and data attribution

BSD 2-Clause (see `LICENSE.txt`). This code started in [GeoServer ConfigOps](https://github.com/Yibbzz/geoserver-configops), where it was the `sync` and `discover` commands.

Data you load keeps its own licence. OS OpenData is released under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) and needs the attribution "Contains OS data © Crown copyright and database right &lt;year&gt;" wherever it's shown.
