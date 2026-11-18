"""`dataops sync`: load new versions of the project's datasets into PostGIS.

For each Dataset that is due (or named on the command line): ask its
source for the current version, and if it is new (or a table is missing,
or `tables` changed), download it, load the layers `tables` names (all of
them if it names none) and swap them in. Every check is written to the
ingest log, which is also what says when a dataset is next due.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import psycopg2
import requests

from .patterns import is_pattern, pattern_stem
from . import discover, ingest, raster, remote, schedule, secrets
from .loader import Project, Resource, default_table, load
from .sources import Product, SourceError
from .validate import validate

DB_KEYS = ("host", "port", "database", "username", "password")
DEFAULT_SCHEMAS = {"live": "data_layers", "staging": "staging", "backup": "backup"}
DEFAULT_CRS = "EPSG:4326"


class SyncError(Exception):
    """Stops the whole command (bad files, no database); `code` is the exit code."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.code = code


def database(project: Resource, root: Path) -> ingest.Database:
    spec = project.spec.get("database") or {}
    secret = (spec.get("connection") or {}).get("secret")
    values = {key: secrets.lookup(secret, key, root) for key in DB_KEYS}
    missing = [key for key, value in values.items() if value is None and key != "port"]
    if missing:
        names = ", ".join(f"${secrets.env_name(secret, key)}" for key in missing)
        raise SyncError(f"the database secret {secret} is missing {', '.join(missing)}: set {names}, or add them to secrets.yaml")
    schemas = {**DEFAULT_SCHEMAS, **(spec.get("schemas") or {})}
    return ingest.Database(
        host=values["host"], port=int(values["port"] or 5432), database=values["database"],
        username=values["username"], password=values["password"], **schemas,
    )


def wanted_tables(dataset: Resource) -> dict[str, dict] | None:
    """`tables`: layer -> options, or None for every layer."""
    tables = dataset.spec.get("tables")
    return {layer: options or {} for layer, options in tables.items()} if tables else None


def plan_layers(dataset: Resource, found: list[tuple[str, str, str | None]], project_crs: str,
                gdal_names: dict[str, str] | None = None) -> list[ingest.Layer]:
    """The layers to load and their tables, from the (source, layer, CRS) a download or service holds.
    `gdal_names` gives GDAL's name for a layer known by another (an ArcGIS layer's, ESRIJSON)."""
    wanted = wanted_tables(dataset)
    by_name = {layer: (source, crs) for source, layer, crs in found}
    if wanted:
        missing = [layer for layer in wanted if layer not in by_name]
        if missing:
            raise ingest.LoadError(
                f"spec.tables names {', '.join(missing)}, which the source doesn't have "
                f"(it has: {', '.join(sorted(by_name))}); `dataops discover` lists them")
        chosen = list(wanted)
    else:
        chosen = [layer for _, layer, _ in found]
    layers = []
    for layer in chosen:
        source, file_crs = by_name[layer]
        options = (wanted or {}).get(layer, {})
        layers.append(ingest.Layer(
            source=source, layer=layer,
            table=options.get("name") or default_table(dataset.name, layer),
            # a CRS the Dataset gives wins; otherwise assign the project's only when the file has none
            crs=options.get("crs") or (None if file_crs else project_crs),
            gdal_layer=(gdal_names or {}).get(layer),
        ))
    tables = [x.table for x in layers]
    clashes = sorted({t for t in tables if tables.count(t) > 1})
    if clashes:
        raise ingest.LoadError(f"two layers would load into the same table: {', '.join(clashes)}; name them in spec.tables")
    return layers


def up_to_date(conn, db: ingest.Database, dataset: Resource, last: ingest.LastLoad | None, version: str | None, sha: str | None) -> bool:
    """Same version (or same file) as the last load, its tables still there, and `tables` unchanged."""
    if last is None or not last.tables:
        return False
    same = (version is not None and last.version == version) or (sha is not None and last.sha256 == sha)
    if not same:
        return False
    wanted = wanted_tables(dataset)
    if wanted:
        names = {o.get("name") or default_table(dataset.name, layer) for layer, o in wanted.items()}
        if names != set(last.tables):
            return False
    return ingest.tables_exist(conn, db.live, last.tables)


def sync_dataset(conn, db: ingest.Database, root: Path, project: Resource, dataset: Resource, force: bool) -> tuple[str, list[str]]:
    """Check one dataset and load it if needed; returns a line for the user, and the tables loaded (none if up to date)."""
    started = datetime.now()
    spec = dataset.spec
    src = discover.resolve(spec["source"], root)
    raster_spec = spec.get("raster")
    if hasattr(src.client, "selection"):  # a STAC collection's or an Overture type's part: options under the type's name
        product: Product = src.client.selection(spec["product"], spec.get(src.type) or spec.get("options") or {})
    elif raster_spec and src.type == "os-downloads":
        product: Product = src.client.product(spec["product"], spec.get("resource"), raster=True)
    else:
        product = src.client.product(spec["url"] if src.type == "url" else spec["product"], spec.get("resource"))
    chosen = product.chosen
    if chosen is None:
        raise ingest.LoadError(f"{product.id} has nothing dataops can load; `dataops discover` shows what it offers")
    last = ingest.last_load(conn, dataset.name)
    if not force and up_to_date(conn, db, dataset, last, product.version, None):
        ingest.record(conn, dataset.name, product.version, chosen.format, "skipped", started, sha=last.sha256, tables=last.tables)
        return f"{dataset.name}: up to date ({discover.version(product.version)})", []

    # End the transaction the lookups above began: a download can take hours, and a transaction left
    # open that long is ended by a server with idle_in_transaction_session_timeout, and holds back vacuum.
    conn.rollback()
    validation = spec.get("validation") or {}
    project_crs = project.spec.get("crs") or DEFAULT_CRS
    if raster_spec:
        return sync_raster(conn, dataset, src, product, last, started, force, project, project_crs)
    with tempfile.TemporaryDirectory(prefix="geoserver-dataops-") as tmp:
        sha, gdal_names = None, None
        if chosen.service:
            connection, gdal_layer, *named = src.client.gdal_source(product)
            name = named[0] if named else gdal_layer
            # A service (WFS, OGC API, ArcGIS) always tells GDAL its layer's CRS: the project's mustn't be put over it
            found, gdal_names = [(connection, name, "declared by the service")], {name: gdal_layer}
        else:
            work = Path(tmp)
            # the download's own name: GDAL names a single file's layer after it (roads.geojson -> roads)
            path = work / (Path(chosen.file_name).name if chosen.file_name else "download")
            if hasattr(src.client, "fetch"):  # a source that writes its file itself (Overture's rows in an area)
                sha = src.client.fetch(chosen, path)
            else:
                sha = ingest.download(src.client.download_url(chosen), path, chosen.md5)
            if not force and up_to_date(conn, db, dataset, last, None, sha):
                version = product.version or f"sha256:{sha[:16]}"
                ingest.record(conn, dataset.name, version, chosen.format, "skipped", started, sha=sha, tables=last.tables)
                return f"{dataset.name}: up to date (same file as last time)", []
            conn.rollback()  # and the one that check began, while the file is unzipped and read
            found = ingest.file_layers(path, work)
            if len(found) == 1 and is_pattern(spec.get("product")):
                # The newest of several files (roads_20261115.zip): its one layer is named after the pattern,
                # not the file, so each new delivery loads into the same table
                source_path, gdal_layer, crs = found[0]
                found, gdal_names = [(source_path, pattern_stem(spec["product"]), crs)], {pattern_stem(spec["product"]): gdal_layer}
        layers = plan_layers(dataset, found, project_crs, gdal_names)
        t0 = time.monotonic()
        staged = ingest.load_all(
            conn, db, layers,
            drop_invalid=validation.get("dropInvalidGeometries", True),
            min_rows=validation.get("minRows", 1),
            keep=project.spec.get("backups", 3),
        )
    version = product.version or (f"sha256:{sha[:16]}" if sha else started.strftime("%Y-%m-%dT%H:%M:%S"))
    rows, dropped = sum(s.rows for s in staged), sum(s.dropped for s in staged)
    tables = [s.layer.table for s in staged]
    ingest.record(conn, dataset.name, version, chosen.format, "success", started,
                  rows=rows, dropped=dropped, sha=sha, tables=tables)
    detail = ", ".join(f"{db.live}.{s.layer.table} ({s.rows:,} rows)" for s in staged)
    notes = ([f"{dropped:,} invalid {'geometry' if dropped == 1 else 'geometries'} left out"] if dropped else [])
    notes += [s.note for s in staged if s.note]
    message = f"{dataset.name}: loaded {discover.version(version)} in {time.monotonic() - t0:.0f}s: {detail}"
    return message + "".join(f"\n  note: {n}" for n in notes), tables


def area_of(dataset: Resource, source_type: str) -> list[float] | None:
    """A selection's area ([west, south, east, north]), which its raster is cropped to: the bbox in the Dataset's block
    for its source type (stac, usgs). Other types have none (a url Dataset's `url` is the file's address)."""
    block = dataset.spec.get(source_type)
    return block.get("bbox") if isinstance(block, dict) else None


def sync_raster(conn, dataset: Resource, src, product: Product, last, started: datetime, force: bool,
                project: Resource, project_crs: str) -> tuple[str, list[str]]:
    """Download a raster product and make it the dataset's COG on the data volume."""
    options, chosen = dataset.spec["raster"], product.chosen
    path = options["path"]
    in_place = not force and last and last.tables == [path] and raster.live_path(path).exists()
    # the source's version says it's unchanged: no need to download gigabytes to find the same file
    if in_place and product.version and last.version == product.version:
        ingest.record(conn, dataset.name, last.version, chosen.format, "skipped", started, sha=last.sha256, tables=[path])
        return f"{dataset.name}: up to date ({discover.version(last.version)})", []
    with tempfile.TemporaryDirectory(prefix="geoserver-dataops-") as tmp:
        work = Path(tmp)
        if product.parts and all(part.remote for part in product.parts):
            # Cloud-Optimised GeoTIFFs read where they are (raster.REMOTE): nothing downloaded; their list and
            # versions (the product's version) stand for them
            downloads = [f"{raster.REMOTE}{src.client.download_url(part)}" for part in product.parts]
            sha = hashlib.sha256(f"{product.version}|{'|'.join(downloads)}".encode()).hexdigest()
        elif product.parts:  # several files, one raster: the digest of theirs stands for them
            (work / "parts").mkdir()
            downloads, hashes = [], []
            for part in product.parts:
                downloads.append(work / "parts" / Path(part.file_name).name)
                if hasattr(src.client, "fetch"):  # a source that downloads its files itself (with a login, say)
                    hashes.append(src.client.fetch(part, downloads[-1]))
                else:
                    hashes.append(ingest.download(src.client.download_url(part), downloads[-1], part.md5))
            sha = hashlib.sha256("".join(hashes).encode()).hexdigest()
        else:
            downloads = [work / (Path(chosen.file_name).name if chosen.file_name else "download")]
            if hasattr(src.client, "fetch"):  # a source that copies its files itself (a folder, a bucket)
                sha = src.client.fetch(chosen, downloads[0])
            else:
                sha = ingest.download(src.client.download_url(chosen), downloads[0], chosen.md5)
        if in_place and last.sha256 == sha:
            # the same file under a new version: recorded, so the next check compares with this version
            version = product.version or last.version
            ingest.record(conn, dataset.name, version, chosen.format, "skipped", started, sha=sha, tables=[path])
            return f"{dataset.name}: up to date (same file{'s' if product.parts else ''} as last time)", []
        t0 = time.monotonic()
        pixels = raster.load(downloads, work, path, options.get("crs"), project_crs, project.spec.get("backups", 3), options.get("nodata"),
                             area=area_of(dataset, src.type))
    version = product.version or f"sha256:{sha[:16]}"
    ingest.record(conn, dataset.name, version, chosen.format, "success", started, rows=pixels, sha=sha, tables=[path])
    return (f"{dataset.name}: loaded {discover.version(version)} in {time.monotonic() - t0:.0f}s: "
            f"{raster.data_root()}/{path} ({pixels:,} pixels)"), [path]


# Failures whose message is written for the user; anything else is shown with its type, as it is likely a bug
EXPECTED = (SourceError, ingest.LoadError, remote.RemoteError, discover.DiscoverError, psycopg2.Error, OSError,
            requests.RequestException)


def failure(e: BaseException) -> str:
    """What to say about a failed dataset, with credentials in quoted addresses masked."""
    text = str(e).strip() or "no details"
    if not isinstance(e, EXPECTED):
        text = f"unexpected {type(e).__name__}: {text} (please report this)"
    return secrets.redact(text)  # a failed download's error quotes its signed address


def usable(conn, db: ingest.Database):
    """The connection, rolled back for the next dataset; a new one if it was lost; None if none can be had."""
    if not conn.closed:
        try:
            conn.rollback()
            return conn
        except psycopg2.Error:
            pass
    try:
        conn.close()
    except psycopg2.Error:
        pass
    try:
        return db.connect()
    except psycopg2.Error:
        return None


def run(root: Path, names: list[str], force: bool = False, loaded_file: Path | None = None) -> int:
    """Check the due (or named) Datasets and load what's new. With `loaded_file`, write there what was
    loaded, one per line: `schema.table` for a table, the path under the data folder for a raster
    (`configops refresh --from <file>` then refreshes GeoServer's layers on them)."""
    project_files = load(root)
    problems = [p for p in validate(project_files) if p.severity == "error"]
    if problems:
        for p in problems[:10]:
            print(f"{p.file}:{p.line}: {p.resource + ': ' if p.resource else ''}{p.message}", file=sys.stderr)
        raise SyncError(f"the files have {len(problems)} error(s); `dataops validate` lists them")
    project = project_files.of_kind("Project")[0]
    datasets = project_files.named("Dataset")
    unknown = [n for n in names if n not in datasets]
    if unknown:
        raise SyncError(f"no Dataset named {', '.join(unknown)} (known: {', '.join(sorted(datasets)) or 'none'})")

    db = database(project, root)
    try:
        conn = db.connect()
    except psycopg2.OperationalError as e:
        raise SyncError(f"can't connect to the database at {db.host}:{db.port}: {str(e).strip().splitlines()[0]}", code=1) from None

    try:
        lock = ingest.hold_lock(db)
    except psycopg2.Error as e:
        conn.close()
        raise SyncError(f"can't take the sync lock: {str(e).strip().splitlines()[0]}", code=1) from None
    if lock is None:
        conn.close()
        raise SyncError(f"another `dataops sync` is loading into this database (staging schema {db.staging}); "
                        "this run changed nothing", code=1)

    failures, everything_loaded = 0, []
    try:
        ingest.bootstrap(conn)
        orphans = ingest.drop_orphans(conn, db)
        if orphans:
            print(f"note: dropped {len(orphans)} staging table(s) an interrupted run left: {', '.join(orphans)}", flush=True)
        pending = list(names or sorted(datasets))
        while pending:
            name = pending.pop(0)
            dataset = datasets[name]
            try:
                update = dataset.spec.get("update")
                if not names and not force and not schedule.due(update, ingest.last_check(conn, name)):
                    print(f"{name}: not due ({update or 'manual'})")
                    continue
                message, loaded = sync_dataset(conn, db, root, project, dataset, force)
                print(message, flush=True)
                raster_path = (dataset.spec.get("raster") or {}).get("path")
                everything_loaded += [t if t == raster_path else f"{db.live}.{t}" for t in loaded]
            except Exception as e:  # noqa: BLE001 - one dataset's failure, whatever it is, mustn't stop the others
                failures += 1
                print(f"{name}: failed: {failure(e)}", file=sys.stderr, flush=True)
                conn = usable(conn, db)
                if conn is None:
                    # the database is gone: nothing more can be loaded, or recorded
                    for rest in pending:
                        failures += 1
                        print(f"{rest}: not checked: lost the connection to the database", file=sys.stderr, flush=True)
                    break
                try:
                    ingest.record(conn, name, "unknown", None, "failed", datetime.now(), error=failure(e))
                except psycopg2.Error as again:
                    conn.rollback()
                    print(f"{name}: and the failure couldn't be recorded: {failure(again)}", file=sys.stderr, flush=True)
    finally:
        if conn is not None:
            conn.close()
        lock.close()
        if loaded_file is not None:
            Path(loaded_file).write_text("".join(f"{x}\n" for x in everything_loaded), encoding="utf-8")
    return 1 if failures else 0
