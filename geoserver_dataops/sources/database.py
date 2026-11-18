"""Tables in another database: your organisation's own data, copied into the PostGIS GeoServer publishes from.

The Source's address says which database: postgresql://host:5432/dbname (PostgreSQL and PostGIS; postgis://
too, and ?sslmode=require) or mysql://host:3306/dbname (MySQL and MariaDB). The login comes from the Source's
secret (username, password), or a user in the address. Each table and view GDAL can read there is a product,
named schema.table (PostgreSQL) or table (MySQL), loaded straight from the database, with no file between.

The version: for a PostgreSQL table, its change counters (rows inserted, updated, deleted since PostgreSQL's
statistics were last reset), so an unchanged table isn't copied again; a view, or a MySQL table whose
UPDATE_TIME the server doesn't keep, has none and is copied each time it is due.
SQL Server and Oracle need client libraries the image doesn't have: not read yet.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from urllib.parse import parse_qs, unquote, urlparse

import requests

from .. import remote
from . import Download, Listing, NotFound, Product, SourceError, matches

KINDS = {"postgresql": "PostgreSQL", "postgres": "PostgreSQL", "postgis": "PostgreSQL", "mysql": "MySQL", "mariadb": "MySQL"}
DEFAULT_PORTS = {"PostgreSQL": 5432, "MySQL": 3306}
INTERNAL = ("topology.", "tiger.", "tiger_data.")  # PostGIS's own tables (its topology and TIGER geocoder extensions)


def quoted(value: str) -> str:
    """A value in a PostgreSQL connection string: quoted when it has spaces or quotes."""
    if value and not any(c in value for c in " '\\"):
        return value
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


class Database:
    name = "database"
    formats = ["PostgreSQL", "MySQL"]

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None,
                 credentials: Callable[[str], str | None] | None = None, options: dict | None = None):
        if not url:
            raise SourceError("a database source needs the database's address: spec.url in the Source, such as "
                              "postgresql://db.example.org:5432/gis or mysql://db.example.org:3306/gis")
        credentials = credentials or (lambda key: None)
        parsed = urlparse(url)
        self.kind = KINDS.get(parsed.scheme)
        if self.kind is None:
            raise SourceError(f"{url}: a database source reads postgresql:// and mysql:// addresses "
                              "(SQL Server and Oracle need client libraries dataops doesn't have yet)")
        self.host, self.port = parsed.hostname or "localhost", parsed.port or DEFAULT_PORTS[self.kind]
        self.dbname = unquote(parsed.path.lstrip("/"))
        if not self.dbname:
            raise SourceError(f"{url}: name the database, as in {parsed.scheme}://{self.host}/<database>")
        self.user = unquote(parsed.username or "") or credentials("username")
        self.password = credentials("password") or unquote(parsed.password or "") or None
        self.sslmode = (parse_qs(parsed.query).get("sslmode") or [None])[0]
        self.url = f"{parsed.scheme}://{self.host}:{self.port}/{self.dbname}"  # as shown: no login in it

    # --- GDAL

    def connection(self) -> str:
        """GDAL's connection string. The password is in it (the loader's own PostGIS password is in PGPASSWORD);
        messages mask it."""
        if self.kind == "PostgreSQL":
            parts = {"host": self.host, "port": str(self.port), "dbname": self.dbname, "user": self.user,
                     "password": self.password, "sslmode": self.sslmode}
            return "PG:" + " ".join(f"{k}={quoted(v)}" for k, v in parts.items() if v)
        parts = [self.dbname, f"host={self.host}", f"port={self.port}"]
        parts += [f"user={self.user}"] if self.user else []
        parts += [f"password={self.password}"] if self.password else []
        return "MYSQL:" + ",".join(parts)

    def _ogrinfo(self, layer: str | None = None) -> list[dict]:
        args = ["ogrinfo", "-ro", "-so", "-json", "-noextent", "-if", self.kind]
        args += ([self.connection(), layer] if layer else ["-nocount", "-al", self.connection()])
        env = {**remote.GDAL_ENV, **os.environ, "PG_LIST_ALL_TABLES": "YES"}  # views and tables without geometry too
        result = subprocess.run(args, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            raise SourceError(f"can't read {self.url}: {self._why(result.stderr)}")
        try:
            return json.loads(result.stdout).get("layers", [])
        except ValueError:
            raise SourceError("ogrinfo gave no JSON output; GDAL 3.7 or later is needed") from None

    def _why(self, stderr: str) -> str:
        """The line of GDAL's output that says why: the server's own (password authentication failed, does not
        exist, refused), not GDAL's closing "unable to open"."""
        lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        reason = next((line.split("FATAL:", 1)[-1].strip() for line in lines
                       if re.search(r"FATAL|denied|refused|does not exist|timed? ?out|could not|Unknown database", line)), None)
        if reason:
            # MySQL puts its reason straight after the connection string, password and all: only the reason
            denied = re.search(r"denied for user .*", reason)
            return f"access {denied.group(0)}" if denied else reason
        if self.kind == "MySQL" and any("connect failed" in line for line in lines):
            return "can't connect or log in: check the address, the login in the Source's secret, and that the server takes connections from here"
        return lines[-1] if lines else "no details"

    # --- versions

    def _versions(self) -> dict[str, str]:
        """table -> a version that changes when its rows do, where the database can say."""
        if self.kind != "PostgreSQL":
            return self._mysql_versions()
        try:
            import psycopg2
            with psycopg2.connect(host=self.host, port=self.port, dbname=self.dbname, user=self.user, password=self.password,
                                  sslmode=self.sslmode or "prefer", connect_timeout=15) as conn, conn.cursor() as cur:
                cur.execute("SELECT schemaname, relname, n_tup_ins, n_tup_upd, n_tup_del FROM pg_stat_user_tables")
                return {f"{s}.{t}": f"changes {i}+{u}+{d}" for s, t, i, u, d in cur.fetchall()}
        except Exception:  # noqa: BLE001 - no statistics (a read-only replica, a login without rights): no versions
            return {}

    def _mysql_versions(self) -> dict[str, str]:
        out = subprocess.run(["ogrinfo", "-ro", "-q", "-json", "-if", "MySQL", self.connection(), "-sql",
                              f"SELECT TABLE_NAME, UPDATE_TIME FROM information_schema.TABLES WHERE TABLE_SCHEMA = '{self.dbname}'"],
                             capture_output=True, text=True, env={**remote.GDAL_ENV, **os.environ})
        try:
            rows = json.loads(out.stdout)["layers"][0]["features"]
        except (ValueError, KeyError, IndexError):
            return {}
        return {f["properties"]["TABLE_NAME"]: str(f["properties"]["UPDATE_TIME"]) for f in rows
                if f["properties"].get("UPDATE_TIME")}

    # --- products

    def _product(self, layer: dict, versions: dict[str, str]) -> Product:
        name = layer["name"]
        key = name if "." in name or self.kind != "PostgreSQL" else f"public.{name}"
        geom = (layer.get("geometryFields") or [{}])[0].get("type") if layer.get("geometryFields") else None
        return Product(id=name, name=name, version=versions.get(key), formats=[self.kind], loads_as=self.kind,
                       description=f"{self.url}: {geom or 'no geometry'}")

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        versions = self._versions()
        found = [p for p in (self._product(layer, versions) for layer in self._ogrinfo()
                             if not layer["name"].startswith(INTERNAL)) if matches(p, search)]
        return Listing(found[:limit] if limit else found, len(found))

    def product(self, name: str, resource: str | None = None) -> Product:
        if resource:
            raise SourceError("a table is one layer; there are no resources to pick")
        layers = {layer["name"]: layer for layer in self._ogrinfo()}
        layer = layers.get(name) or (layers.get(name.removeprefix("public.")) if self.kind == "PostgreSQL" else None)
        if layer is None:
            raise NotFound(name)
        p = self._product(layer, self._versions())
        download = Download(format=self.kind, url=self.url, name=layer["name"], id=layer["name"], service=True)
        p.downloads, p.chosen = [download], download
        return p

    def layers(self, product: Product) -> list[remote.Layer]:
        return remote.to_layers(self._ogrinfo(product.chosen.id if product.chosen else product.id), "")

    def gdal_source(self, product: Product) -> tuple[str, str]:
        return self.connection(), product.chosen.id if product.chosen else product.id

    def download_url(self, download: Download) -> str:
        return self.url
