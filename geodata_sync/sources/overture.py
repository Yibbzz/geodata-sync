"""Overture Maps: open map data for the whole world (buildings, roads, places, addresses, boundaries, land),
released monthly as GeoParquet on public cloud storage.

Each theme's type is a product (`buildings/building`, `places/place`, `transportation/segment`); its version
is the release (`2026-10-21.0`), so a Dataset loads again once a month, when Overture publishes. A Dataset
names the area it wants (`overture.bbox`): the world's buildings are billions of rows.

Only what is needed is read, over HTTP range requests: each file's footer, then the row groups whose
bounding boxes (Overture keeps them per row group) overlap the area. The rows inside the area are written
as GeoJSON sequence, which GDAL loads as any download; columns that hold structures (names, sources) become
JSON. Reading Parquet needs pyarrow and shapely: pip install 'geodata-sync[overture]' (the image has them).
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

from . import Download, Listing, NotFound, Product, SourceError, http_session, matches

BUCKET = "https://overturemaps-us-west-2.s3.us-west-2.amazonaws.com"
TIMEOUT = 60
S3 = "{http://s3.amazonaws.com/doc/2006-03-01/}"
FORMAT = "GeoParquet"
WORKERS = 8  # files read at once
BBOX = ("xmin", "xmax", "ymin", "ymax")


def libraries():
    try:
        import pyarrow.compute as pc
        import pyarrow.parquet as pq
        import shapely
    except ImportError:
        raise SourceError("the overture source reads GeoParquet, which needs pyarrow and shapely: "
                          "pip install 'geodata-sync[overture]' (the geodata-sync image has them)") from None
    return pq, pc, shapely


def area_of(options: dict) -> tuple[float, float, float, float]:
    west, south, east, north = options["bbox"]
    if west >= east or south >= north:
        raise SourceError(f"overture.bbox {options['bbox']} is empty: give [west, south, east, north], in degrees")
    return west, south, east, north


class Overture:
    name = "Overture Maps"
    formats = [FORMAT]

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        self.base = (url or BUCKET).rstrip("/")  # a mirror of the bucket, if given
        self.session = session or http_session()

    # --- the bucket's listing (S3 ListObjectsV2, anonymous)

    def _list(self, prefix: str, folders: bool) -> tuple[list[str], list[tuple[str, int]]]:
        """(sub-folders, (key, size) of files) under `prefix`."""
        found_folders, files, token = [], [], None
        while True:
            params = {"list-type": "2", "prefix": prefix, **({"delimiter": "/"} if folders else {})}
            if token:
                params["continuation-token"] = token
            try:
                r = self.session.get(self.base + "/", params=params, timeout=TIMEOUT)
            except requests.RequestException as e:
                raise SourceError(f"can't reach {self.base}: {e}") from None
            if r.status_code != 200:
                raise SourceError(f"{self.base} answered HTTP {r.status_code} listing {prefix}")
            try:
                root = ET.fromstring(r.content)
            except ET.ParseError:
                raise SourceError(f"{self.base} didn't answer with a bucket listing") from None
            found_folders += [p.text for p in root.iter(f"{S3}Prefix") if p.text and p.text != prefix
                              and p.text.startswith(prefix)]
            for c in root.iter(f"{S3}Contents"):
                key, size = c.findtext(f"{S3}Key"), c.findtext(f"{S3}Size") or "0"
                if key and key.endswith(".parquet"):
                    files.append((key, int(size)))
            if root.findtext(f"{S3}IsTruncated") != "true":
                return found_folders, files
            token = root.findtext(f"{S3}NextContinuationToken")

    def release(self) -> str:
        releases = [p.split("/")[1] for p in self._list("release/", True)[0]]
        if not releases:
            raise SourceError(f"{self.base} has no Overture releases (release/<date>/)")
        return max(releases)  # named by date: 2026-10-21.0

    def _types(self, release: str) -> list[str]:
        found = []
        for theme in self._list(f"release/{release}/", True)[0]:
            for kind in self._list(theme, True)[0]:
                m = re.search(r"theme=([^/]+)/type=([^/]+)/$", kind)
                if m:
                    found.append(f"{m.group(1)}/{m.group(2)}")
        return sorted(found)

    def _product(self, product_id: str, release: str) -> Product:
        theme, kind = product_id.split("/")
        download = Download(format=FORMAT, url=f"{self.base}/release/{release}/theme={theme}/type={kind}/",
                            file_name=f"{kind}.geojsonl", id=product_id, name=product_id)
        return Product(id=product_id, name=f"{theme.capitalize()}: {kind.replace('_', ' ')}", version=release,
                       formats=[FORMAT], downloads=[download], loads_as=FORMAT, chosen=download,
                       description=f"Overture Maps {theme} theme, release {release}")

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        release = self.release()
        found = [p for p in (self._product(t, release) for t in self._types(release)) if matches(p, search)]
        return Listing(found[:limit] if limit else found, len(found))

    def product(self, product_id: str, resource: str | None = None) -> Product:
        if resource:
            raise SourceError("an Overture type has no resources to pick; a Dataset names its area with overture.bbox")
        if not re.fullmatch(r"[a-z_]+/[a-z_]+", product_id):
            raise SourceError(f"'{product_id}' isn't an Overture theme and type, such as buildings/building "
                              "(`geodata discover overture` lists them)")
        release = self.release()
        theme, kind = product_id.split("/")
        if not self._list(f"release/{release}/theme={theme}/type={kind}/", True)[1]:
            raise NotFound(product_id)
        return self._product(product_id, release)

    def selection(self, product_id: str, options: dict) -> Product:
        """The type, in a Dataset's area: a new area is a new version, so it loads again."""
        p = self.product(product_id)
        area = area_of(options)
        p.chosen.area = json.dumps(list(area))
        p.version = f"{p.version} ({hashlib.sha256(p.chosen.area.encode()).hexdigest()[:8]})"
        return p

    def download_url(self, download: Download) -> str:
        return download.url

    # --- reading: the rows in the area, as GeoJSON sequence

    def fetch(self, download: Download, dest: Path) -> str:
        """Write the rows of `download` (a type's folder) inside its area to `dest`; returns the SHA-256 of what was written."""
        pq, pc, shapely = libraries()
        from ..remote import HttpFile

        west, south, east, north = json.loads(download.area) if download.area else (-180, -90, 180, 90)
        prefix = download.url.removeprefix(self.base + "/")
        keys = [key for key, _ in self._list(prefix, False)[1]]
        if not keys:
            raise SourceError(f"no files in {download.url}")
        lock, sha, counts = threading.Lock(), hashlib.sha256(), {"rows": 0, "groups": 0}

        def overlaps(meta, group: int) -> bool:
            stats = {}
            rg = meta.row_group(group)
            for c in range(rg.num_columns):
                col = rg.column(c)
                if col.path_in_schema in ("bbox.xmin", "bbox.xmax", "bbox.ymin", "bbox.ymax") and col.statistics \
                        and col.statistics.has_min_max:
                    stats[col.path_in_schema.split(".")[1]] = (col.statistics.min, col.statistics.max)
            if len(stats) < 4:
                return True  # no statistics: read it to find out
            return (stats["xmax"][1] >= west and stats["xmin"][0] <= east
                    and stats["ymax"][1] >= south and stats["ymin"][0] <= north)

        def read(key: str, out) -> None:
            with io.BufferedReader(HttpFile(f"{self.base}/{key}", self.session), buffer_size=1 << 20) as f:
                pf = pq.ParquetFile(f)
                columns = [n for n in pf.schema_arrow.names if n != "bbox"]
                for group in range(pf.metadata.num_row_groups):
                    if not overlaps(pf.metadata, group):
                        continue
                    table = pf.read_row_group(group, columns=columns + ["bbox"])
                    box = table.column("bbox")
                    inside = pc.and_(pc.and_(pc.greater_equal(pc.struct_field(box, "xmax"), west),
                                             pc.less_equal(pc.struct_field(box, "xmin"), east)),
                                     pc.and_(pc.greater_equal(pc.struct_field(box, "ymax"), south),
                                             pc.less_equal(pc.struct_field(box, "ymin"), north)))
                    table = table.filter(inside).select(columns)
                    if table.num_rows == 0:
                        continue
                    geometries = shapely.to_geojson(shapely.from_wkb(table.column("geometry").to_numpy(zero_copy_only=False)))
                    rows = table.drop_columns(["geometry"]).to_pylist()
                    lines = "".join(
                        '{"type":"Feature","geometry":' + g + ',"properties":' + json.dumps(r, default=str, ensure_ascii=False) + "}\n"
                        for g, r in zip(geometries, rows)).encode()
                    with lock:
                        out.write(lines)
                        sha.update(lines)
                        counts["rows"] += table.num_rows
                        counts["groups"] += 1

        with open(dest, "wb") as out, ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for done in [pool.submit(read, key, out) for key in keys]:
                try:
                    done.result()
                except requests.RequestException as e:
                    raise SourceError(f"couldn't read {download.url}: {e}") from None
        if counts["rows"] == 0:
            raise SourceError(f"nothing of {download.id} is inside {[west, south, east, north]}")
        return sha.hexdigest()
