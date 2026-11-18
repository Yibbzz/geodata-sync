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
**Sources**, by the `type` a Source names. Green: run against the real service; yellow: only against fakes of the cloud's endpoints (no accounts on the test machine); grey: not run yet (it needs a key). [docs/spec.md](docs/spec.md#source) says what each reads.

Portals and catalogues: ![CKAN: ckan](https://img.shields.io/badge/CKAN-ckan-brightgreen) ![Socrata: socrata](https://img.shields.io/badge/Socrata-socrata-brightgreen) ![OpenDataSoft: opendatasoft](https://img.shields.io/badge/OpenDataSoft-opendatasoft-brightgreen) ![ArcGIS Server and Online: arcgis](https://img.shields.io/badge/ArcGIS_Server_and_Online-arcgis-brightgreen) ![ArcGIS Hub: ogc-api-records](https://img.shields.io/badge/ArcGIS_Hub-ogc--api--records-brightgreen) ![OGC API Records: ogc-api-records](https://img.shields.io/badge/OGC_API_Records-ogc--api--records-brightgreen) ![CSW (GeoNetwork): csw](https://img.shields.io/badge/CSW_%28GeoNetwork%29-csw-brightgreen) ![INSPIRE Atom: atom](https://img.shields.io/badge/INSPIRE_Atom-atom-brightgreen) ![Ordnance Survey: os-downloads](https://img.shields.io/badge/Ordnance_Survey-os--downloads-brightgreen) ![Koordinates: koordinates](https://img.shields.io/badge/Koordinates-koordinates-lightgrey)<br>
Web services: ![WFS: wfs](https://img.shields.io/badge/WFS-wfs-brightgreen) ![OGC API Features: ogc-api-features](https://img.shields.io/badge/OGC_API_Features-ogc--api--features-brightgreen)<br>
Imagery and world map data: ![STAC (Earth Search): stac](https://img.shields.io/badge/STAC_%28Earth_Search%29-stac-brightgreen) ![STAC (Planetary Computer): stac](https://img.shields.io/badge/STAC_%28Planetary_Computer%29-stac-brightgreen) ![Overture Maps: overture](https://img.shields.io/badge/Overture_Maps-overture-brightgreen) ![OpenStreetMap (Geofabrik): geofabrik](https://img.shields.io/badge/OpenStreetMap_%28Geofabrik%29-geofabrik-brightgreen) ![OpenStreetMap (Overpass): overpass](https://img.shields.io/badge/OpenStreetMap_%28Overpass%29-overpass-brightgreen)<br>
Files: ![A link: url](https://img.shields.io/badge/A_link-url-brightgreen) ![A folder or share: files](https://img.shields.io/badge/A_folder_or_share-files-brightgreen) ![SFTP: files](https://img.shields.io/badge/SFTP-files-brightgreen) ![FTP: files](https://img.shields.io/badge/FTP-files-brightgreen) ![Amazon S3 and S3-compatible: files](https://img.shields.io/badge/Amazon_S3_and_S3--compatible-files-brightgreen) ![Google Cloud Storage: files](https://img.shields.io/badge/Google_Cloud_Storage-files-brightgreen) ![Azure Blob Storage: files](https://img.shields.io/badge/Azure_Blob_Storage-files-brightgreen) ![AWS, Google and Azure identities: files](https://img.shields.io/badge/AWS%2C_Google_and_Azure_identities-files-yellow)<br>
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
docker run --rm ghcr.io/yibbzz/geoserver-dataops --help  # or the image, GDAL included
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

**Refresh GeoServer after a load.** `sync --loaded` lists what it loaded, and `configops refresh` works out those layers' bounds again and clears their cached tiles:

```bash
dataops sync -C my-data --loaded loaded.txt
configops refresh -C my-geoserver --from loaded.txt
```

**Keep logins out of the files.** A file names a Secret (`connection: { secret: database }`); its value comes from a Kubernetes Secret, a `DATAOPS_<SECRET>_<KEY>` environment variable or a git-ignored `secrets.yaml`. Credentials in addresses are masked in errors and the ingest log.

**Run it in Kubernetes** as a nightly CronJob that loads, then refreshes GeoServer: [docs/kubernetes.md](docs/kubernetes.md).

## Built to be safe to run

- **A load that fails changes nothing.** Live tables are only swapped once every table of the dataset has loaded and passed its checks.
- **The old data is kept.** Each swap moves the old table to a backup schema; the newest `backups` are kept.
- **What reads the tables keeps working.** The new table gets the old one's grants, and views on it are moved across.
- **One sync at a time.** A run takes a lock in PostgreSQL, so a nightly job and a person can't load through each other.
- **Downloads aren't trusted.** GDAL may only open them as data formats, and what an archive unpacks to is limited.
- **Checked before it runs.** `dataops validate` reads the files without a network; problems come out as `file:line`.

## What to know

- Big layers over WFS or OGC API Features don't load reliably: use a file download when there is one ([docs/sources-notes.md](docs/sources-notes.md)).
- A WFS or OGC API Features source has no version to compare, so it is loaded in full each time it is due; so is an ArcGIS layer that doesn't report its last edit.
- Overture reads GeoParquet with pyarrow: `pip install 'geoserver-dataops[overture]'` (the image has it). Koordinates needs an API key.
- A materialized view on a live table stops its load: drop it before and create it again after.

## Documentation

| | |
| --- | --- |
| [docs/spec.md](docs/spec.md) | The file format and every command |
| [docs/kubernetes.md](docs/kubernetes.md) | Running it nightly in a cluster |
| [docs/sources-notes.md](docs/sources-notes.md) | What we learned about the sources |
| [docs/development.md](docs/development.md) | Tests, CI, new GeoServer versions, releasing |

## Licence

MIT (see `LICENSE`). Data you load keeps its own licence: OS OpenData is under the [Open Government Licence v3.0](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/) and needs "Contains OS data © Crown copyright and database right &lt;year&gt;" wherever it's shown.
