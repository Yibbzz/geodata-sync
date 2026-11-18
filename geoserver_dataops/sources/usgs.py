"""The USGS National Map (tnmaccess.nationalmap.gov): the US government's elevation (3DEP), hydrography (3DHP),
boundaries, structures, transportation, geographic names and more, through its own product API.

A product is one of its datasets, by name (`National Elevation Dataset (NED) 1/3 arc-second`, as discover
lists them), and a Dataset names its area (`usgs.bbox`, degrees) and, if the dataset comes in several, the
format (`usgs.format`: GeoTIFF, GeoPackage, Shapefile, FileGDB). The API answers with the files covering the
area, every edition of each tile: the newest of each is used.

Rasters (elevation) are Cloud-Optimised GeoTIFFs, read where they are on USGS's storage: only the area is
fetched, then mosaicked and cropped to it (a 1/3 arc-second tile is 400 MB; a few square kilometres of it, a MB
or so). Vector datasets come as files per state or watershed: the one that fits the area best is loaded.
The version is the files and their publication dates, so a new edition of any tile loads again.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from urllib.parse import urlparse

import requests

from . import Download, Listing, NotFound, Product, SourceError, http_session, matches

API = "https://tnmaccess.nationalmap.gov/api/v1"
TIMEOUT = 120
PAGE = 100
MAX_FILES = 60  # more than this is too big an area for one load: a smaller bbox
RASTER_FORMATS = ("geotiff", "tiff", "img")


def tile_key(item: dict) -> str:
    """The tile an item is an edition of: its file name without the date (USGS_13_n40w106_20110601.tif -> USGS_13_n40w106)."""
    name = PurePosixPath(urlparse(item.get("downloadURL") or "").path).stem
    return re.sub(r"_\d{8}$", "", name) or str(item.get("sourceId") or item.get("title"))


class USGS:
    name = "USGS National Map"
    formats = ["GeoTIFF", "GeoPackage", "Shapefile", "File Geodatabase"]

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        self.url = (url or API).rstrip("/")
        self.session = session or http_session()

    def _get(self, path: str, what: str, params: dict | None = None):
        try:
            r = self.session.get(f"{self.url}/{path}", params=params, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SourceError(f"can't reach {self.url}: {e}") from None
        if r.status_code != 200:
            raise SourceError(f"{self.url} answered HTTP {r.status_code} for {what}")
        try:
            return r.json()
        except ValueError:
            raise SourceError(f"{self.url} didn't answer with JSON for {what}") from None

    # --- the datasets

    def _datasets(self) -> list[tuple[str, str, list[str]]]:
        """(dataset name, its group's title, formats) for every dataset the API searches by name."""
        found = []

        def walk(node: dict, group: str, formats: list[str]) -> None:
            for tag in node.get("tags") or []:
                if not isinstance(tag, dict):
                    continue
                own = [f.get("value") for f in tag.get("formats") or [] if isinstance(f, dict) and f.get("value")] or formats
                if tag.get("sbDatasetTag"):
                    found.append((tag["sbDatasetTag"], group, own))
                walk(tag, group, own)

        for group in self._get("datasets", "the datasets") or []:
            formats = [f.get("value") for f in group.get("formats") or [] if isinstance(f, dict) and f.get("value")]
            before = len(found)
            walk(group, group.get("title") or "", formats)
            if len(found) == before and group.get("sbDatasetTag"):  # a group with no datasets inside is one itself (structures, names)
                found.append((group["sbDatasetTag"], group.get("title") or "", formats))
        return found

    @staticmethod
    def _product(name: str, group: str, formats: list[str]) -> Product:
        shown = sorted({f.split(",")[0].strip() for f in formats if f.strip()})
        return Product(id=name, name=name, version=None, formats=shown, loads_as=", ".join(shown) or None,
                       description=f"{group}; a Dataset names its area (usgs.bbox)")

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        found = [p for p in (self._product(*d) for d in self._datasets()) if matches(p, search)]
        return Listing(found[:limit] if limit else found, len(found))

    def product(self, name: str, resource: str | None = None) -> Product:
        if resource:
            raise SourceError("a National Map dataset has no resources to pick; usgs.format picks the format")
        datasets = self._datasets()
        for found in datasets:
            if found[0] == name:
                return self._product(*found)
        raise SourceError(f"the National Map has no dataset {name!r}: `dataops discover usgs` lists them by name "
                          f"(e.g. {datasets[0][0]!r})" if datasets else f"the National Map has no dataset {name!r}")

    # --- the files in an area

    def _items(self, name: str, bbox: list[float], fmt: str | None) -> list[dict]:
        west, south, east, north = bbox
        if west >= east or south >= north:
            raise SourceError(f"usgs.bbox {bbox} is empty: give [west, south, east, north], in degrees")
        items, offset = [], 0
        while True:
            params = {"datasets": name, "bbox": f"{west},{south},{east},{north}", "max": PAGE, "offset": offset}
            if fmt:
                params["prodFormats"] = fmt
            page = self._get("products", f"{name}'s files", params)
            got = [i for i in page.get("items") or [] if isinstance(i, dict) and i.get("downloadURL")]
            items += got
            offset += PAGE
            if not got or offset >= int(page.get("total") or 0):
                return items

    def selection(self, name: str, options: dict) -> Product:
        p = self.product(name)
        items = self._items(name, options["bbox"], options.get("format"))
        if not items:
            raise SourceError(f"the National Map has no {name} files in {options['bbox']}"
                              + (f" as {options['format']}" if options.get("format") else ""))
        newest: dict[str, dict] = {}
        for item in items:
            key = tile_key(item)
            if key not in newest or str(item.get("publicationDate")) > str(newest[key].get("publicationDate")):
                newest[key] = item
        chosen = sorted(newest.values(), key=tile_key)
        if len(chosen) > MAX_FILES:
            raise SourceError(f"{len(chosen)} files cover {options['bbox']}: more than {MAX_FILES}; make usgs.bbox smaller")
        rasters = [i for i in chosen if str(i.get("format", "")).lower() in RASTER_FORMATS]
        if rasters and len(rasters) == len(chosen):
            p.parts = [Download(format="GeoTIFF", url=i["downloadURL"], file_name=PurePosixPath(urlparse(i["downloadURL"]).path).name,
                                size=i.get("sizeInBytes"), id=tile_key(i), name=i.get("title") or "",
                                remote=i["downloadURL"].lower().endswith((".tif", ".tiff")))
                       for i in rasters]
            p.downloads, p.chosen, p.loads_as = p.parts, p.parts[0], "GeoTIFF"
        else:  # files per state or watershed: the one that fits the area best
            best = max(chosen, key=lambda i: float(i.get("bestFitIndex") or 0))
            d = Download(format=str(best.get("format") or ""), url=best["downloadURL"],
                         file_name=PurePosixPath(urlparse(best["downloadURL"]).path).name, size=best.get("sizeInBytes"),
                         id=tile_key(best), name=best.get("title") or "")
            p.downloads, p.chosen, p.loads_as = [d], d, d.format
        stamp = json.dumps(sorted((tile_key(i), str(i.get("publicationDate"))) for i in chosen) + [options.get("format")])
        p.version = f"{len(chosen)} files, {hashlib.sha256(stamp.encode()).hexdigest()[:12]}"
        return p

    def download_url(self, download: Download) -> str:
        return download.url
