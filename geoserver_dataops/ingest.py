"""Loading tables into PostGIS: stage, check, then swap in, with backups.

Every layer is loaded with ogr2ogr into the staging schema under a unique
name, checked (invalid geometries dropped, row count), then all of a
dataset's tables go live in one transaction: each old table is renamed into
the backup schema and the new one renamed into place. Nothing is copied,
and if anything fails before the swap, every live table stays as it was.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import psycopg2
import psycopg2.errors
import requests

from . import remote

LOG_TABLE = "public.os_ingest_log"  # the name the first ingest pipeline gave it; kept so history carries on
TIMEOUT = 120

# A date PostgreSQL rejects (shapefiles store "no date" as 0000/00/00): load dates as text instead
BAD_DATE = re.compile(r"date/time field value out of range|invalid input syntax for type (date|time|timestamp)|column \w+: \"0000", re.IGNORECASE)
DATES_AS_TEXT = ["-mapFieldType", "Date=String,Time=String,DateTime=String"]

BOOTSTRAP_SQL = f"""
CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
    id              SERIAL PRIMARY KEY,
    dataset_id      TEXT        NOT NULL,
    version         TEXT        NOT NULL,
    format_used     TEXT,
    status          TEXT        NOT NULL,   -- success | failed | skipped
    rows_loaded     BIGINT,
    rows_filtered   BIGINT,                 -- invalid geometries left out
    sha256          TEXT,
    error           TEXT,
    started_at      TIMESTAMP   NOT NULL DEFAULT now(),
    finished_at     TIMESTAMP
);
ALTER TABLE {LOG_TABLE} ADD COLUMN IF NOT EXISTS tables TEXT;
-- every run and `dataops status` look up each dataset's latest rows
CREATE INDEX IF NOT EXISTS os_ingest_log_dataset_started ON {LOG_TABLE} (dataset_id, started_at);
"""


# Most a download may unpack to, so a zip bomb can't fill the disk (DATAOPS_MAX_UNPACKED_BYTES overrides)
MAX_UNPACKED = 100 * 1000**3
MAX_UNPACKED_ENV = "DATAOPS_MAX_UNPACKED_BYTES"


class LoadError(Exception):
    """A load failed in a way the user can act on; the message says how."""


@dataclass
class Database:
    host: str
    port: int
    database: str
    username: str
    password: str
    live: str = "data_layers"
    staging: str = "staging"
    backup: str = "backup"

    def connect(self):
        return psycopg2.connect(host=self.host, port=self.port, dbname=self.database, user=self.username, password=self.password)

    def ogr_target(self) -> str:
        """For ogr2ogr; the password goes in the environment, not the command line."""
        return f"PG:host={self.host} port={self.port} dbname={self.database} user={self.username}"


@dataclass
class Layer:
    """One layer to load: where GDAL reads it, and the live table it becomes."""

    source: str  # a file path, or a connection such as WFS:<url>
    layer: str
    table: str
    crs: str | None = None  # assigned when given (the file declares none, or the Dataset overrides it)
    gdal_layer: str | None = None  # GDAL's name for the layer, when `layer` is another (ArcGIS's are all ESRIJSON)


@dataclass
class Staged:
    layer: Layer
    staging: str
    rows: int = 0
    dropped: int = 0
    note: str = ""


@dataclass
class LastLoad:
    version: str
    sha256: str | None
    tables: list[str] = field(default_factory=list)


def stamp(when: datetime | None = None) -> str:
    return (when or datetime.now()).strftime("%Y%m%d_%H%M%S")


def suffixed(table: str, suffix: str) -> str:
    """<table>_<suffix> within PostgreSQL's 63-character limit."""
    return f"{table[: 62 - len(suffix)]}_{suffix}"


# The end of every staging table's name: _<YYYYmmdd_HHMMSS>_<6 hex digits> (staging_name)
STAGING_TAG = "_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$"


def hold_lock(db: Database):
    """
    A connection holding the lock that lets one sync at a time load through `db`'s staging schema,
    or None if another run holds it. The lock is PostgreSQL's (an advisory lock), so it works across
    machines and is let go when the connection closes, however the run ends.
    """
    conn = db.connect()
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext('geoserver-dataops'), hashtext(%s))", (db.staging,))
        if cur.fetchone()[0]:
            return conn
    conn.close()
    return None


def drop_orphans(conn, db: Database) -> list[str]:
    """
    Drop staging tables a run that was killed (out of memory, a node lost) left behind; returns their
    names. Only call with the lock held: then no other run is loading, so any staging table is an orphan.
    Only tables named as staging_name names them are touched.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_name ~ %s "
                    "ORDER BY table_name", (db.staging, STAGING_TAG))
        orphans = [row[0] for row in cur.fetchall()]
        for name in orphans:
            cur.execute(f'DROP TABLE IF EXISTS "{db.staging}"."{name}"')
    conn.commit()
    return orphans


def staging_name(table: str) -> str:
    """
    A name for this load's staging table, unique to the load. ogr2ogr names
    the table's key, index and sequence after it (…_ogc_fid_seq is the
    longest), and they move with the table into live and backup, so it is
    kept short enough that those names aren't cut short and stay unique.
    """
    tag = f"{stamp()}_{uuid.uuid4().hex[:6]}"
    return f"{table[: 63 - len('_ogc_fid_seq') - len(tag) - 1]}_{tag}"


# --- the ingest log


def bootstrap(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(BOOTSTRAP_SQL)
    conn.commit()


def last_load(conn, dataset: str) -> LastLoad | None:
    """The version, checksum and tables of what is live: the last load, or a later check that found the same
    data under a new version (a portal's metadata changed, its file didn't), so that one isn't downloaded again."""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT version, sha256, tables FROM {LOG_TABLE} WHERE dataset_id = %s AND status IN ('success', 'skipped') "
            "AND tables IS NOT NULL "
            "ORDER BY finished_at DESC LIMIT 1",
            (dataset,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return LastLoad(row[0], row[1], [t for t in (row[2] or "").split(",") if t])


def last_check(conn, dataset: str) -> datetime | None:
    """When the dataset was last checked; a failed attempt doesn't count, so the next run tries again."""
    with conn.cursor() as cur:
        cur.execute(f"SELECT max(started_at) FROM {LOG_TABLE} WHERE dataset_id = %s AND status <> 'failed'", (dataset,))
        return cur.fetchone()[0]


def record(conn, dataset: str, version: str, fmt: str | None, status: str, started_at: datetime,
           rows: int | None = None, dropped: int | None = None, sha: str | None = None,
           error: str | None = None, tables: list[str] | None = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {LOG_TABLE} (dataset_id, version, format_used, status, rows_loaded, rows_filtered, "
            "sha256, error, started_at, finished_at, tables) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s)",
            (dataset, version, fmt, status, rows, dropped, sha, error[:1000] if error else None, started_at,
             ",".join(tables) if tables else None),
        )
    conn.commit()


def tables_exist(conn, schema: str, tables: list[str]) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s AND table_name = ANY(%s)",
            (schema, tables),
        )
        return cur.fetchone()[0] == len(set(tables))


# --- getting the data


# A connection lost part way through a download is tried again, from where it stopped when the
# server can send part of a file: one dropped connection shouldn't cost a night's load of a large product
DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_PAUSE = 10  # seconds, times the attempt
DROPPED = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)


def download(url: str, dest: Path, md5: str | None = None, session: requests.Session | None = None) -> str:
    """Stream `url` to `dest`; check the MD5 when the source gives one; return the SHA256."""
    sha, md5sum, written = hashlib.sha256(), hashlib.md5(), 0
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        try:
            headers = {"Range": f"bytes={written}-"} if written else {}
            with (session or requests).get(url, stream=True, timeout=TIMEOUT, headers=headers) as r:
                r.raise_for_status()
                resumed = bool(written) and r.status_code == 206 and \
                    r.headers.get("Content-Range", "").startswith(f"bytes {written}-")
                if not resumed:  # from the start: the first try, or a server that sends whole files only
                    sha, md5sum, written = hashlib.sha256(), hashlib.md5(), 0
                with open(dest, "ab" if resumed else "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                        sha.update(chunk)
                        md5sum.update(chunk)
                        written += len(chunk)
            break
        except DROPPED:
            if attempt == DOWNLOAD_ATTEMPTS:
                raise
            time.sleep(DOWNLOAD_PAUSE * attempt)
    if md5 and md5sum.hexdigest().lower() != md5.lower():
        raise LoadError(f"the download's MD5 is {md5sum.hexdigest()}, not {md5} as the source says; try again later")
    return sha.hexdigest()


def unpack_limit() -> int:
    value = os.environ.get(MAX_UNPACKED_ENV, "")
    return int(value) if value.isdigit() else MAX_UNPACKED


def check_unpacked_size(size: int) -> None:
    """Refuse an archive that would unpack to more than the limit. A zip's entries can't unpack to
    more than the sizes it declares (zipfile stops there), so their sum is a true upper bound."""
    if size > unpack_limit():
        raise LoadError(f"the download would unpack to {size:,} bytes, more than the limit of {unpack_limit():,}; "
                        f"if that is right for this dataset, raise {MAX_UNPACKED_ENV}")


def file_layers(path: Path, work: Path) -> list[tuple[str, str, str | None]]:
    """(GDAL path, layer, CRS) of every layer in a downloaded file, unzipping it first."""
    if zipfile.is_zipfile(path):
        out = work / "extracted"
        with zipfile.ZipFile(path) as z:
            check_unpacked_size(sum(i.file_size for i in z.infolist()))
            z.extractall(out)
            files = [str(out / f) for f in remote.data_files([i.filename for i in z.infolist() if not i.is_dir()])]
        if not files:
            raise LoadError("the download holds no data files GDAL can read")
    else:
        files = [str(path)]
    found = []
    for f in files:
        for layer in remote.to_layers(remote.ogrinfo(f), f):
            found.append((f, layer.name, layer.crs))
    return found


# --- loading


def ensure_schemas(conn, *schemas: str) -> None:
    with conn.cursor() as cur:
        for schema in schemas:
            cur.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    conn.commit()


def gdal_errors(stderr: str) -> str:
    """The lines of GDAL's output that say what went wrong, without its boilerplate."""
    lines = []
    for line in stderr.splitlines():
        line = line.strip()
        if not line or "Terminating translation" in line or "use -skipfailures" in line or line in lines:
            continue
        if line.startswith(("ERROR", "CONTEXT", "DETAIL")):
            lines.append(line.removeprefix("ERROR 1: ").removeprefix("ERROR:").strip())
    return "; ".join(lines[-3:]) or (stderr.strip().splitlines() or ["no details"])[-1]


def load(db: Database, layer: Layer, staging: str) -> str:
    """ogr2ogr one layer into <staging schema>.<staging>; returns a note for the user, or ""."""
    if shutil.which("ogr2ogr") is None:
        raise LoadError("loading needs GDAL's ogr2ogr on the PATH")
    cmd = [
        "ogr2ogr", "-f", "PostgreSQL", *remote.input_drivers(layer.source), db.ogr_target(), layer.source, layer.gdal_layer or layer.layer,
        "-nln", f"{db.staging}.{staging}",
        "-lco", "GEOMETRY_NAME=geom", "-lco", "PRECISION=NO",
        "-nlt", "PROMOTE_TO_MULTI", "-gt", "65536",
        "--config", "PG_USE_COPY", "YES",
    ]
    if layer.crs:
        cmd += ["-a_srs", layer.crs]
    env = {**remote.GDAL_ENV, **os.environ, "PGPASSWORD": db.password}
    result = subprocess.run(cmd + ["-overwrite"], capture_output=True, text=True, env=env)
    if result.returncode != 0 and BAD_DATE.search(result.stderr):
        result = subprocess.run(cmd + ["-overwrite"] + DATES_AS_TEXT, capture_output=True, text=True, env=env)
        if result.returncode == 0:
            return f"{layer.table}: dates loaded as text, since some aren't valid dates"
    if result.returncode != 0:
        raise LoadError(f"ogr2ogr couldn't load layer {layer.layer}: {gdal_errors(result.stderr)}")
    return ""


def geometry_column(conn, schema: str, table: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT f_geometry_column FROM geometry_columns WHERE f_table_schema = %s AND f_table_name = %s LIMIT 1",
                    (schema, table))
        row = cur.fetchone()
    return row[0] if row else None


def check(conn, db: Database, staged: Staged, drop_invalid: bool, min_rows: int) -> None:
    """Drop invalid geometries if asked, count rows, and refuse a table below `min_rows`."""
    name = f'"{db.staging}"."{staged.staging}"'
    with conn.cursor() as cur:
        geom = geometry_column(conn, db.staging, staged.staging)
        if drop_invalid and geom:
            cur.execute(f'DELETE FROM {name} WHERE NOT ST_IsValid("{geom}")')
            staged.dropped = cur.rowcount
        cur.execute(f"SELECT count(*) FROM {name}")
        staged.rows = cur.fetchone()[0]
    conn.commit()
    if staged.rows < min_rows:
        raise LoadError(f"{staged.layer.table} would have {staged.rows:,} rows, fewer than minRows ({min_rows}); "
                        "the live table is unchanged")
    with conn.cursor() as cur:
        cur.execute(f"ANALYZE {name}")
    conn.commit()


LOCK_TIMEOUT = "10s"  # how long a swap waits for readers of the live table before stepping back
LOCK_ATTEMPTS = 6
LOCK_PAUSE = 5  # seconds between attempts


def dependent_views(cur, table: str) -> list[tuple[int, str, str, str, list[str]]]:
    """(oid, kind, name, definition, options) of the views that read `table` ("schema"."table") directly.
    Call with an empty search_path, so the names and definitions come fully qualified."""
    cur.execute(
        "SELECT DISTINCT v.oid, v.relkind, v.oid::regclass::text, pg_get_viewdef(v.oid), coalesce(v.reloptions, '{}') "
        "FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid JOIN pg_class v ON v.oid = r.ev_class "
        "WHERE d.classid = 'pg_rewrite'::regclass AND d.refclassid = 'pg_class'::regclass "
        "AND d.refobjid = to_regclass(%s) AND v.oid <> d.refobjid ORDER BY v.oid", (table,))
    return cur.fetchall()


def copy_grants(cur, old: str, new: str) -> None:
    """Give `new` the privileges granted on `old`: a table ogr2ogr just made has only its owner's."""
    cur.execute(
        "SELECT CASE WHEN a.grantee = 0 THEN 'PUBLIC' ELSE a.grantee::regrole::text END, a.privilege_type, a.is_grantable "
        "FROM pg_class c, aclexplode(c.relacl) a WHERE c.oid = to_regclass(%s) AND a.grantee <> c.relowner", (old,))
    for role, privilege, grantable in cur.fetchall():
        cur.execute(f"GRANT {privilege} ON {new} TO {role}" + (" WITH GRANT OPTION" if grantable else ""))


def swap_once(conn, db: Database, staged: list[Staged]) -> None:
    with conn.cursor() as cur:
        # an empty search_path makes PostgreSQL write view definitions with every name qualified
        cur.execute("SET LOCAL search_path = ''")
        cur.execute("SET LOCAL lock_timeout = %s", (LOCK_TIMEOUT,))
        views: dict[int, tuple[str, str, list[str]]] = {}
        for s in staged:
            live = f'"{db.live}"."{s.layer.table}"'
            for oid, kind, name, definition, options in dependent_views(cur, live):
                if kind != "v":
                    raise LoadError(
                        f"materialized view {name} reads {db.live}.{s.layer.table}, and can't be moved onto the new table: "
                        "it would go on reading the old one. Drop it before the load and create it again after; "
                        "the live table is unchanged")
                views[oid] = (name, definition, options)
        for s in staged:
            table = s.layer.table
            cur.execute("SELECT to_regclass(%s)", (f'"{db.live}"."{table}"',))
            if cur.fetchone()[0] is not None:
                copy_grants(cur, f'"{db.live}"."{table}"', f'"{db.staging}"."{s.staging}"')
                backup = free_backup_name(cur, db, table)
                cur.execute(f'ALTER TABLE "{db.live}"."{table}" RENAME TO "{backup}"')
                cur.execute(f'ALTER TABLE "{db.live}"."{backup}" SET SCHEMA "{db.backup}"')
            cur.execute(f'ALTER TABLE "{db.staging}"."{s.staging}" SET SCHEMA "{db.live}"')
            cur.execute(f'ALTER TABLE "{db.live}"."{s.staging}" RENAME TO "{table}"')
        # A view is tied to the table it was made on, not to its name: left alone it would follow the
        # old table into the backups and serve old data. Its definition, taken before the renames,
        # names the live table, so replacing the view with it points it at the new one; the view
        # keeps its owner and grants.
        for name, definition, options in views.values():
            with_options = f" WITH ({', '.join(options)})" if options else ""
            try:
                cur.execute(f"CREATE OR REPLACE VIEW {name}{with_options} AS {definition}")
            except psycopg2.errors.LockNotAvailable:
                raise
            except psycopg2.Error as e:
                raise LoadError(f"view {name} can't be moved onto the new table ({str(e).strip().splitlines()[0]}); "
                                "the live table is unchanged") from None
    conn.commit()


def swap(conn, db: Database, staged: list[Staged], keep: int) -> None:
    """In one transaction: each live table to the backup schema, each staged table into its place,
    with the old table's grants and the views that read it."""
    for attempt in range(1, LOCK_ATTEMPTS + 1):
        try:
            swap_once(conn, db, staged)
            break
        except psycopg2.errors.LockNotAvailable:
            # Waiting on behind a long query would hold up every other reader of the table too
            conn.rollback()
            if attempt == LOCK_ATTEMPTS:
                raise LoadError(f"the live tables stayed in use for {LOCK_ATTEMPTS} tries of {LOCK_TIMEOUT}; "
                                "they are unchanged, and the next run tries again") from None
            time.sleep(LOCK_PAUSE)
    for s in staged:
        # the new data is live: a backup that can't be dropped mustn't make the load a failure
        problem = prune(conn, db, s.layer.table, keep)
        if problem:
            s.note = "; ".join(x for x in (s.note, problem) if x)


def free_backup_name(cur, db: Database, table: str) -> str:
    """<table>_<now>, a second later if two loads land in the same second."""
    when = datetime.now()
    while True:
        name = suffixed(table, stamp(when))
        cur.execute("SELECT to_regclass(%s)", (f'"{db.backup}"."{name}"',))
        if cur.fetchone()[0] is None:
            return name
        when += timedelta(seconds=1)


def prune(conn, db: Database, table: str, keep: int) -> str:
    """Drop all but the newest `keep` backups of a table; returns what couldn't be dropped, or ""."""
    prefix = table[: 62 - len("YYYYmmdd_HHMMSS")]
    kept = []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s AND table_name ~ %s "
            "ORDER BY table_name DESC",
            (db.backup, f"^{prefix}_[0-9]{{8}}_[0-9]{{6}}$"),
        )
        for (old,) in cur.fetchall()[keep:]:
            try:
                cur.execute("SAVEPOINT prune")
                cur.execute(f'DROP TABLE "{db.backup}"."{old}"')
            except psycopg2.Error as e:
                cur.execute("ROLLBACK TO SAVEPOINT prune")
                kept.append(f"old backup {db.backup}.{old} was kept: {str(e).strip().splitlines()[0]}")
    conn.commit()
    return "; ".join(kept)


def drop_staged(conn, db: Database, staged: list[Staged]) -> None:
    conn.rollback()
    with conn.cursor() as cur:
        for s in staged:
            cur.execute(f'DROP TABLE IF EXISTS "{db.staging}"."{s.staging}"')
    conn.commit()


def load_all(conn, db: Database, layers: list[Layer], drop_invalid: bool = True, min_rows: int = 1, keep: int = 3) -> list[Staged]:
    """Stage and check every layer, then swap them all in; on any failure, live tables are untouched."""
    ensure_schemas(conn, db.live, db.staging, db.backup)
    staged: list[Staged] = []
    try:
        for layer in layers:
            s = Staged(layer, staging_name(layer.table))
            staged.append(s)
            s.note = load(db, layer, s.staging)
            check(conn, db, s, drop_invalid, min_rows)
        swap(conn, db, staged, keep)
    except BaseException:
        try:
            drop_staged(conn, db, staged)
        except psycopg2.Error:
            pass  # the connection is gone: let the error that got us here be the one reported
        raise
    return staged
