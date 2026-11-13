# geodata-sync files (`geodata/v1alpha1`)

A set of YAML files that says which open datasets you want, where they come from and how often they update. `geodata` reads them and keeps PostGIS up to date: it downloads each new version, loads it into staging tables, checks it, backs up the old tables and swaps the new ones in, in one transaction.

Publishing the tables (GeoServer layers, styles, caching) is another tool's job: [GeoServer ConfigOps](https://github.com/Yibbzz/geoserver-configops) publishes them from its own files, and `configops refresh` brings GeoServer up to date after a load.

## Principles

1. **The files hold what you chose, nothing else.** Versions, row counts and load times live in the ingest log in the database, not in the files.
2. **Load safely.** A new version is staged, checked (row counts, invalid geometries left out) and swapped in at once; the old tables are kept as backups.
3. **Secrets by reference.** API keys and passwords never appear in the files, only the name of the secret that holds them.
4. **Shaped like Kubernetes resources** (`apiVersion`, `kind`, `metadata`, `spec`).
5. **Build on existing tools.** Reading and loading go through GDAL (`ogr2ogr`, `gdal_translate`).

## Files

`geodata` reads every `*.yaml` file under one directory (`secrets.yaml` aside). A file can hold several resources separated by `---`:

```
uk-basemap/
  project.yaml              # one Project: the database
  sources.yaml              # Sources
  datasets/
    os-open-roads.yaml      # Datasets
    boundary-line.yaml
```

## Kinds

### Project

One per directory: the database to load into, and defaults.

```yaml
apiVersion: geodata/v1alpha1
kind: Project
metadata:
  name: uk-basemap
spec:
  database:
    connection: { secret: database }               # keys: host, port, database, username, password
    schemas: { live: data_layers, staging: staging, backup: backup }
  crs: EPSG:27700                                  # default CRS for files that don't say
  backups: 3                                       # previous loads kept per table
```

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `database.connection.secret` | yes | | Secret with `host`, `port`, `database`, `username`, `password` |
| `database.schemas` | no | `data_layers`, `staging`, `backup` | Where live, staged and backed-up tables live |
| `crs` | no | `EPSG:4326` | Default CRS of tables |
| `backups` | no | `3` | Previous versions kept per table |

### Source

Where data comes from. One per provider account.

```yaml
apiVersion: geodata/v1alpha1
kind: Source
metadata:
  name: os
spec:
  type: os-downloads                 # OS Downloads API
  credentials: { secret: os-api }    # key: apiKey; optional, OpenData products need none
---
apiVersion: geodata/v1alpha1
kind: Source
metadata:
  name: web
spec:
  type: url                          # plain file downloads, no key
---
apiVersion: geodata/v1alpha1
kind: Source
metadata:
  name: data-gov-uk
spec:
  type: ckan                         # a CKAN portal
  url: https://data.gov.uk
```

| Field | Required | Meaning |
| --- | --- | --- |
| `type` | yes | One of the types below |
| `url` | for `ckan`, `ogc-api-features`, `wfs` | The source's address. For CKAN, the portal (`https://data.gov.uk`); an address already inside `/api/` (such as `https://portal.example/api/3/action`) is used as given. For WFS, the service (`https://host/wfs`); request parameters copied with it (`?service=WFS&request=GetCapabilities`) are dropped, others (`?map=`) kept. For OGC API Features, the API's landing page; a collection's address is cut back to it |
| `credentials.secret` | no | Secret with `apiKey`. For CKAN it is an API token, sent only to the portal, never to the servers its files are on. Not yet used by `wfs` or `ogc-api-features`, which work with public services only so far |

| `type` | Lists products (`discover`) | Needs a key | Status |
| --- | --- | --- | --- |
| `os-downloads` | yes | no for OpenData products | `discover` built; loading exists in the pipeline |
| `url` | no: you give the URL | no | `discover` built (reads the file's layers) |
| `ckan` (data.gov.uk and many city, regional and national portals) | yes, by search | no for public data | `discover` built |
| `wfs` (versions 1.0 to 2.0) | yes: feature types | usually no | `discover` built |
| `ogc-api-features` | yes: feature collections | usually no | `discover` built |

Each type is a plug-in with two parts: list what's available, and fetch one product with its version. Adding a provider means adding one plug-in.

### Dataset

One product from a source, loaded into one or more tables (or, with `raster:`, made into one Cloud-Optimised GeoTIFF), with an update schedule.

```yaml
apiVersion: geodata/v1alpha1
kind: Dataset
metadata:
  name: os-open-roads
spec:
  source: os
  product: OpenRoads                 # the provider's product ID, as `geodata discover os` lists it
  update: weekly                     # daily | weekly | monthly | manual | a cron expression
  tables:                            # source layer -> table; names as `geodata discover os OpenRoads` lists them
    road_link: {}                    # table os_open_roads_road_link
    road_node: { name: road_nodes }  # table road_nodes
```

| Field | Required | Default | Meaning |
| --- | --- | --- | --- |
| `source` | yes | | Name of a Source |
| `product` | for `os-downloads`, `ckan`, `wfs` | | Product, package or collection ID at the source |
| `url` | for `url` | | File to download (zip, GeoPackage, GeoJSON, CSV, Shapefile) |
| `resource` | no, `ckan` only | the best loadable file | Which file of the package: a resource ID or name, as `discover` lists them. Packages often hold the same data in several formats, or several files |
| `update` | no | `manual` | When `sync` checks for a new version |
| `tables` | no | every layer in the file | Which layers of the downloaded file to load, and as what |
| `tables.<layer>.name` | no | `<dataset>_<layer>` with dashes as underscores | Table name in the live schema |
| `tables.<layer>.crs` | no | from the file, else the project's `crs` | Override when the file doesn't declare its CRS |
| `validation.dropInvalidGeometries` | no | `true` | Leave out rows whose geometry PostGIS calls invalid |
| `validation.minRows` | no | `1` | Fail the update (keep the old table) below this count |

A product is a *version* at the source (OS publishes `2026-10`, for example). `sync` loads a new version only when the source has one, and keeps the last `backups` versions of each table.

#### Raster Datasets

With `raster:` instead of `tables:`, the download becomes one Cloud-Optimised GeoTIFF in a data folder (`/data`, the folder GeoServer's file stores read; configops publishes it with a `Store` whose `path` is the same):

```yaml
apiVersion: geodata/v1alpha1
kind: Dataset
metadata:
  name: terrain-50
spec:
  source: os
  product: Terrain50                 # OS's ASCII Grid download: ~2,800 tiles in zips inside a zip
  update: monthly
  raster:
    path: rasters/os-terrain-50.tif  # under /data
    crs: EPSG:27700                  # the grids carry no CRS of their own
```

| Field | Default | Meaning |
| --- | --- | --- |
| `raster.path` | required | Where the COG goes, under the data volume; ends in `.tif` |
| `raster.crs` | the rasters' own, else the project's `crs` | Assigned to the result |
| `raster.nodata` | none | The value written where no raster covers (GDAL would write 0 there), and read as "no data" in the rasters |

`sync` downloads the product (for `os-downloads`, a raster format: GeoTIFF, then ASCII Grid; for `ckan`, name the file with `resource`), unpacks every raster in it, through zips inside zips, mosaics them through a GDAL VRT and writes one deflate-compressed COG with overviews. It writes beside the live file, checks GDAL can read it, then renames it over the live file, so GeoServer never reads half a file; the old one moves to `.geodata-backups/` beside it, and the newest `backups` are kept. `GEODATA_DATA_DIR` stands in for `/data` where the folder is mounted elsewhere.

## Secrets

A `secret` field names a secret; the files never hold its value. Where `geodata` looks, for secret `os-api`, key `apiKey`:

| Running | Where it comes from |
| --- | --- |
| On a laptop or in CI | Environment variable `GEODATA_OS_API_APIKEY`, or a git-ignored `secrets.yaml` next to the project (`os-api: { apiKey: ... }`) |
| In Kubernetes | Files `$GEODATA_SECRETS_DIR/os-api/apiKey`: the Secret mounted as a folder |

## Ownership

Only tables a Dataset declares, in the Project's schemas, are created, replaced or backed up; nothing else in the database is touched.

## Commands

| Command | Does |
| --- | --- |
| `geodata validate` | Checks the files: schema, references between resources. No network |
| `geodata discover <source> [product]` | Lists products a source offers, or the layers inside one product |
| `geodata sync [dataset]` | Checks sources for new versions and loads them (stage, validate, back up, swap in). The nightly job |
| `geodata status` | Per dataset: version loaded, when, rows, last error, next check |

### `discover`

`geodata discover <source>` lists a source's products and the format each would load as; `geodata discover <source> <product>` lists its downloads, marks the one `sync` would use, and lists the layers inside it, which are the names `tables` takes. `<source>` is a Source in the project (`-C dir`, default the current directory), whose secret supplies the API key if it has one, or a source type when there is no project yet: `os-downloads`, `ckan --url <portal>`, or `url`.

| Source type | `discover <source>` | `discover <source> <product>` |
| --- | --- | --- |
| `os-downloads` | Every OS product (`--search` narrows) | The product's formats; layers of the one `sync` uses |
| `ckan` | Packages matching `--search`, 20 at a time (`--limit`); without a search, the latest changed | The package's resources, marked loadable or not (web pages and services aren't); layers of the best file, or of `--resource <id or name>` |
| `url` | (nothing to list) | The product is the file's address: `geodata discover url https://example.org/roads.zip` |
| `wfs` | The service's feature types (`--search` narrows) | The feature type (with or without its namespace prefix): geometry, CRS and count, read by GDAL's WFS driver, which also loads it, so the layer name shown is the loader's |
| `ogc-api-features` | The API's feature collections (coverages and records are left out) | The collection: the count from `numberMatched`, the geometry from the first feature, the CRS the data is stored in |

On a CKAN package, a resource that is a WFS or OGC API service (recognised by its format, address or title, since portals often leave the format blank) shows the command that discovers it, and search results mark packages with only a service as `via WFS`. Many data.gov.uk datasets are published this way, the Environment Agency's among them:

```
$ geodata discover ogc-api-features --url https://environment.data.gov.uk/spatialdata/flood-map-for-planning-flood-zones/ogc/features/v1 Flood_Zones_2_3_Rivers_and_Sea
Flood Zones 2 3 Rivers And Sea (Flood_Zones_2_3_Rivers_and_Sea)
  OGC API Features: https://environment.data.gov.uk/spatialdata/flood-map-for-planning-flood-zones/ogc/features/v1/collections/Flood_Zones_2_3_Rivers_and_Sea/items

LAYER                           GEOMETRY      CRS         FEATURES
Flood_Zones_2_3_Rivers_and_Sea  MultiPolygon  EPSG:27700  415,408
```

Services are asked directly, so nothing is cached. Requests to any source are tried again, twice, when a server drops the connection or is briefly unavailable.

```
$ geodata discover data-gov-uk conservation-areas125
Conservation Areas (conservation-areas125), version 2026-11-04
    Shapefile: Conservation Areas, conservation_areas.zip, size unknown  [c3a73b80-a7fb-4e21-ab32-e87f514b620e]
    GeoJSON: Conservation Areas, conservation_areas.geojson, size unknown  [5dcaa616-b91c-4e58-8b11-141fda4db7e1]
    GML: Conservation Areas, conservation_areas.gml, size unknown  [57f9f97a-82da-4e98-b5a4-7f9d2dff75ee]
  * GeoPackage: Conservation Areas, conservation_areas.gpkg, size unknown  [9dd40b81-62f5-4c75-b1cd-d200af101632]
  (* the download sync uses)
Another one: --resource <id or name>; in a Dataset, `resource: 9dd40b81-62f5-4c75-b1cd-d200af101632`

LAYER               GEOMETRY  CRS         FEATURES
conservation_areas  Geometry  EPSG:27700  11
```

Portals label formats loosely (`SHP`, `ZIP`, a media-type URL, or nothing), so the format comes from the declared format, the media type and the file name together; a zip named `roads.gpkg.zip` counts as a GeoPackage. Loadable files, best first: GeoPackage, Shapefile, GeoJSON, GML, File Geodatabase, KML, FlatGeobuf, then a zip of unknown contents.

```
$ geodata discover os OpenRoads
OS Open Roads (OpenRoads), version 2026-10
    ESRI® Shapefile: oproad_essh_gb.zip, 585.8 MB
    GML 3: oproad_gml3_gb.zip, 611.8 MB
  * GeoPackage: oproad_gpkg_gb.zip, 1.0 GB
    Vector Tiles (MBTiles): oproad_mbtiles_gb.zip, 1.4 GB
  (* the download sync uses)

LAYER              GEOMETRY    CRS         FEATURES
motorway_junction  Point       EPSG:27700  668
road_link          LineString  EPSG:27700  3,980,725
road_node          Point       EPSG:27700  3,364,565
```

The layers are read from the download in place, with GDAL over HTTP range requests, so nothing is saved to disk (a server that only sends whole files has the file fetched to a temporary folder instead, up to 300 MB); the zip's file list costs a few requests, but a GeoPackage inside a compressed zip is read through, which takes a minute or two for a 1 GB product. A product version never changes (for CKAN, the package's last change; for a link, its ETag or Last-Modified), so the layers are cached (`~/.cache/configops`) and the next run is instant; `--refresh` reads them again, `--no-layers` skips them, `--format json` is for scripts. GDAL 3.7 or later (`ogrinfo`) is needed for layers.

The download `sync` uses is the first of GeoPackage, Shapefile, GML, MapInfo TAB that comes as one file for the whole country; a format offered only as grid-square tiles is passed over, since each tile would become its own table.

### `sync`

`geodata sync` loads new versions of the project's datasets into PostGIS. Run with no names (the nightly job), it checks each Dataset whose `update` schedule is due; `geodata sync roads rivers` checks those two whatever their schedule, and `--force` loads even when nothing changed.

```
$ geodata sync -C examples/uk-basemap
boundary-line: not due (monthly)
conservation-areas: loaded 2026-11-04 in 1s: data_layers.conservation_areas (10 rows)
  note: 1 invalid geometry left out
flood-zones: not due (monthly)
os-open-roads: up to date (2026-10)
```

For each dataset it checks:

1. **Is there a new version?** It asks the source for its current version: OS's release, a CKAN package's last change, a link's ETag. If that matches the last load, its tables still exist and `tables` hasn't changed, there's nothing to do. A source with no version is downloaded and compared by checksum. A service (WFS, OGC API) is loaded each time it is due.
2. **Download and stage:** the file is downloaded (with OS's MD5 checked) and unzipped. Every layer `tables` names (all of them if it names none) is loaded with ogr2ogr into the staging schema. A service is loaded straight from the service. A layer whose file declares no CRS gets the project's `crs`, or the table's own `crs`.
3. **Check:** invalid geometries are left out (unless `validation.dropInvalidGeometries: false`), and a table with fewer than `validation.minRows` rows stops the load.
4. **Swap in:** in one transaction, each live table is renamed into the backup schema and the new one renamed into its place. Nothing is copied, and if any table fails its check, none of the dataset's live tables change. The newest `backups` backups of each table are kept.

`--loaded FILE` writes what was loaded, one per line: `schema.table` for a table, the path under the data folder for a raster. GeoServer still has the old bounding boxes and cached tiles of layers on them; `configops refresh --from FILE` (GeoServer ConfigOps) brings them up to date.

Every check (loaded, up to date, failed) is written to the ingest log in `public.os_ingest_log`, which is how the next run knows the version and when the dataset is next due. A failed check doesn't count, so the next run tries again. Schedules allow an hour's slack, so a daily dataset checked a little after 02:00 yesterday is due at 02:00 today; a cron expression is due when it has named a minute since the last check.

The database comes from the Project's `database.connection` secret (keys `host`, `port`, `database`, `username`, `password`), so on a laptop: `GEODATA_DATABASE_HOST=localhost` and so on, or `secrets.yaml`.

In Kubernetes, run it nightly as a CronJob ([kubernetes.md](kubernetes.md)).

Known gaps:

- Some data needs fixing in flight: a shapefile with impossible dates (`0000/00/00`) has its date columns loaded as text, with a note.
- Some WFS servers can't serve features in pages, and fail. The BGS geology service is one: it serves complex (GeoSciML) features and asks for a sort order.
- WFS and OGC API Features suit small and medium layers. A large one is fetched page by page, each page slower than the last on servers that skip ahead by offset, and one failed page fails the load. The EA's flood zones (415k features) took hours over its OGC API and kept failing; its 1 GB file download loads in minutes. Use the file when there is one.
- Private CKAN files, which need the API token on the file's own server, can't be downloaded yet.

## `status`

`geodata status` shows each dataset from the ingest log, and changes nothing:

```
$ geodata status
DATASET             UPDATE  LOADED      AT                ROWS  NEXT CHECK        LAST CHECK
conservation-areas  weekly  2026-11-04  2026-11-10 13:53  10    2026-11-17 12:53  success
os-open-roads       weekly  never       -                 -     now               failed: can't reach the OS Downloads API: …
```

`--format json` is for scripts and dashboards.
