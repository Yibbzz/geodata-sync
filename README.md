<p align="center"><img src="docs/logo.svg" alt="GeoServer DataOps" width="120"></p>

<h1 align="center">GeoServer DataOps</h1>

<p align="center">The data GeoServer publishes, loaded into PostGIS and kept up to date, from YAML files.<br>For <a href="https://github.com/geoserver/geoserver-cloud">GeoServer Cloud</a> and standard GeoServer, beside <a href="https://github.com/Yibbzz/geoserver-configops">GeoServer ConfigOps</a>.</p>

<!-- badges -->
![GeoServer Cloud 3.0.2.1](https://img.shields.io/badge/GeoServer_Cloud-3.0.2.1-brightgreen) ![GeoServer 3.0.2](https://img.shields.io/badge/GeoServer-3.0.2-brightgreen) ![GeoServer 2.28.6](https://img.shields.io/badge/GeoServer-2.28.6-brightgreen) ![PostGIS 18-3.7](https://img.shields.io/badge/PostGIS-18--3.7-blue)
<!-- /badges -->

<!-- tested-on -->
Tested on **GeoServer Cloud 3.0.2.1**, **GeoServer 3.0.2** and **GeoServer 2.28.6**, with **PostGIS 18-3.7**: on every pull request each one publishes what `dataops sync` loaded and serves it back, before and after a reload, and a daily job (`.github/workflows/versions.yaml`) tries each new release.
<!-- /tested-on -->

<!-- sources -->
**Sources**: each checked against its real service [every day](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) (a small sync, or a listing where products are large), so a provider changing its API shows here as failing. The `type` a Source names for each is in [docs/spec.md](docs/spec.md#source). Some can't be checked without an account: the cloud identities and the logins to NASA Earthdata and Copernicus Data Space are tested against fakes of their endpoints, and Koordinates needs a key.

Portals and catalogues: [![CKAN](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fckan.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![Socrata](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fsocrata.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![OpenDataSoft](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fopendatasoft.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![ArcGIS](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Farcgis.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![ArcGIS Hub](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Farcgis-hub.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![data.europa.eu](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fdata-europa.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![GeoNode](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fgeonode.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![CSW](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fcsw.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![INSPIRE Atom](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fatom.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![Ordnance Survey](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fos-downloads.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) ![Koordinates: not tested (needs a key)](https://img.shields.io/badge/Koordinates-not_tested_%28needs_a_key%29-lightgrey)<br>
Web services: [![WFS](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fwfs.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![OGC API Features](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fogc-api-features.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml)<br>
Imagery and world map data: [![STAC, Earth Search](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fstac-earth-search.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![STAC, Planetary Computer](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fstac-planetary-computer.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![Overture Maps](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Foverture.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![Geofabrik](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fgeofabrik.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![Overpass](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Foverpass.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![USGS National Map, elevation](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fusgs.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![USGS National Map, vector](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fusgs-vector.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) ![STAC, NASA Earthdata: login: fakes only](https://img.shields.io/badge/STAC%2C_NASA_Earthdata-login%3A_fakes_only-yellow) ![STAC, Copernicus Data Space: login: fakes only](https://img.shields.io/badge/STAC%2C_Copernicus_Data_Space-login%3A_fakes_only-yellow)<br>
Files: [![A link](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Furl.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![Spreadsheets](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fspreadsheet.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![A folder or share](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Ffiles-folder.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![SFTP](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Ffiles-sftp.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![FTP](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Ffiles-ftp.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![Amazon S3](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Ffiles-s3.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![Google Cloud Storage](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Ffiles-gcs.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![Azure Blob Storage](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Ffiles-azure.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) ![AWS, Google and Azure identities: fakes only](https://img.shields.io/badge/AWS%2C_Google_and_Azure_identities-fakes_only-yellow)<br>
Databases: [![PostgreSQL / PostGIS](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fdatabase-postgresql.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml) [![MySQL / MariaDB](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2FYibbzz%2Fgeoserver-dataops%2Fbadges%2Fsources%2Fdatabase-mysql.json)](https://github.com/Yibbzz/geoserver-dataops/actions/workflows/sources.yaml)<br>
Anything else: [![Your own: plugin](https://img.shields.io/badge/Your_own-plugin-blue)](docs/plugins.md)
<!-- /sources -->

`dataops` keeps the open datasets you name in PostGIS, each at its newest version:

- **Find** data in the places it is published, worldwide: the sources above, or anything else as a [plugin](docs/plugins.md), a class in your own package. `discover` lists what a source offers and the layers inside a download.
- **Load** it safely: staged, checked (invalid geometries, row counts), then swapped in for the old tables in one transaction, which are kept as backups. A raster download becomes one Cloud-Optimised GeoTIFF.
- **Keep it up to date.** Each dataset has a schedule; `sync`, run nightly, checks those that are due and loads only a new version.

It loads the data; [GeoServer ConfigOps](https://github.com/Yibbzz/geoserver-configops) configures GeoServer to publish it, and `configops refresh` brings GeoServer up to date after each load.

> **Status: 0.x.** The file format may still change between releases. Formerly **geodata-sync**: the command was `geodata`, the files' `apiVersion` `geodata/v1alpha1` and the variables `GEODATA_*`; [docs/development.md](docs/development.md#renamed-from-geodata-sync) says what to change.

## Install

```bash
pip install geoserver-dataops                            # Python 3.10+, and GDAL's tools (apt install gdal-bin)
pip install 'geoserver-dataops[overture,sftp,google]'    # with what Overture, SFTP and Google service accounts need
docker run --rm ghcr.io/yibbzz/geoserver-dataops --help  # or the image: GDAL and all of those included
```

## Example

[`examples/quickstart/project.yaml`](examples/quickstart/project.yaml): Surrey Heath's conservation areas, from data.gov.uk, checked monthly.

```yaml
apiVersion: dataops/v1alpha1
kind: Project
metadata: { name: quickstart }
spec:
  database:
    connection: { secret: database }     # keys: host, port, database, username, password
  crs: EPSG:27700
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: { name: data-gov-uk }
spec: { type: ckan, url: https://data.gov.uk }
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: { name: conservation-areas }
spec:
  source: data-gov-uk
  product: conservation-areas125         # `dataops discover data-gov-uk --search "conservation areas"` finds it
  update: monthly
  tables:
    conservation_areas: {}               # the layer in the download; table conservation_areas_conservation_areas
```

```console
$ docker compose -f examples/compose.yaml up -d --wait

$ cat > examples/quickstart/secrets.yaml <<'EOF'
database: { host: localhost, port: "5433", database: postgres, username: postgres, password: postgres }
EOF

$ dataops sync -C examples/quickstart
  conservation-areas: loaded 2026-11-11 in 0s: data_layers.conservation_areas_conservation_areas (10 rows)
    note: 1 invalid geometry left out
$ dataops status -C examples/quickstart
  DATASET             UPDATE   LOADED      AT                ROWS  NEXT CHECK        LAST CHECK
  conservation-areas  monthly  2026-11-11  2026-11-13 08:51  10    2026-12-13 07:51  success
$ dataops sync -C examples/quickstart
  conservation-areas: not due (monthly)
```

`docker compose -f examples/compose.yaml down -v` removes the database. Two larger examples: [`examples/uk-basemap`](examples/uk-basemap/) (OS Open Roads, Boundary-Line and the Environment Agency's flood zones: 3 GB, 20 minutes) and [`examples/terrain`](examples/terrain/) (OS Terrain 50 as one Cloud-Optimised GeoTIFF).

## Common tasks

**Find a dataset** and the layers inside it:

```bash
dataops discover os-downloads                                   # what a source type offers
dataops discover data-gov-uk --search "flood zones" -C my-data  # search a Source of the project
dataops discover data-gov-uk <product> -C my-data               # its files, and their layers
```

**Load now** rather than when due: `dataops sync -C my-data <dataset>`; `--force` loads even when the version hasn't changed.

**Load your own data**: a table in another database, the files in a folder, an SFTP server or a bucket. A pattern
loads the newest of a supplier's deliveries into the same table each time:

```yaml
apiVersion: dataops/v1alpha1
kind: Source
metadata: { name: assets }
spec: { type: database, url: "postgresql://db.example.org:5432/assets", credentials: { secret: assets } }
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: { name: supplier }
spec:
  type: files
  url: sftp://drop@files.supplier.example/deliveries/   # or /mnt/share/, s3://bucket/prefix/, gs://, az://
  credentials: { secret: supplier }
  options: { hostKey: "SHA256:..." }                    # ssh-keyscan files.supplier.example | ssh-keygen -lf -
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: { name: roads }
spec: { source: supplier, product: "roads_*.zip", update: daily }
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: { name: valves }
spec: { source: assets, product: network.valves, update: "0 * * * *" }   # hourly; copied only when the table changed
```

**Add a source of your own** (your organisation's API) as a [plugin](docs/plugins.md): a class in a package installed beside dataops.

**Refresh GeoServer after a load.** `sync --loaded` lists what it loaded, and `configops refresh` works out those layers' bounds again and clears their cached tiles:

```bash
dataops sync -C my-data --loaded loaded.txt
configops refresh -C my-geoserver --from loaded.txt
```

**Keep logins out of the files.** A file names a Secret (`connection: { secret: database }`); its value comes from a Kubernetes Secret, a `DATAOPS_<SECRET>_<KEY>` environment variable or a git-ignored `secrets.yaml`. Buckets can use the cloud's own identity instead (IRSA, EKS Pod Identity, GKE and AKS workload identity, instance roles and managed identities). Credentials in addresses and connection strings are masked in errors and the ingest log.

**Run it in Kubernetes** as a nightly CronJob that loads, then refreshes GeoServer: [docs/kubernetes.md](docs/kubernetes.md).

## Built to be safe to run

- **A load that fails changes nothing.** Live tables are only swapped once every table of the dataset has loaded and passed its checks.
- **The old data is kept.** Each swap moves the old table to a backup schema; the newest `backups` are kept.
- **What reads the tables keeps working.** The new table gets the old one's grants, and views on it are moved across.
- **One sync at a time.** A run takes a lock in PostgreSQL, so a nightly job and a person can't load through each other.
- **Downloads aren't trusted.** GDAL may only open them as data formats, and what an archive unpacks to is limited.
- **Checked before it runs.** `dataops validate` reads the files without a network; problems come out as `file:line`.
- **Only what's needed is fetched.** Cloud-Optimised GeoTIFFs (USGS elevation) are read in place for the area alone, a selection is cropped to its area, and an unchanged file or table isn't fetched again.
- **Every source is checked daily** against its real service, so a provider changing its API shows on the badges above.

## What to know

- Big layers over WFS or OGC API Features don't load reliably: use a file download when there is one ([docs/sources-notes.md](docs/sources-notes.md)).
- A WFS or OGC API Features source has no version to compare, so it is loaded in full each time it is due; so is an ArcGIS layer that doesn't report its last edit.
- Koordinates needs an API key; NASA Earthdata a token; Copernicus Data Space an account. GeoNode is read for its public datasets.
- A database source reads PostgreSQL/PostGIS and MySQL/MariaDB; SQL Server and Oracle need client libraries the image doesn't have. A view, which has no version, is copied each time it is due.
- A materialized view on a live table stops its load: drop it before and create it again after.

## Documentation

| | |
| --- | --- |
| [docs/spec.md](docs/spec.md) | The file format and every command |
| [docs/kubernetes.md](docs/kubernetes.md) | Running it nightly in a cluster |
| [docs/sources-notes.md](docs/sources-notes.md) | What we learned about the sources |
| [docs/plugins.md](docs/plugins.md) | Adding a source type of your own |
| [docs/development.md](docs/development.md) | Tests, CI, new GeoServer versions, releasing |

## Licence

MIT (see `LICENSE`). Data you load keeps its own licence: OS OpenData is under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) and needs "Contains OS data © Crown copyright and database right &lt;year&gt;" wherever it's shown.
