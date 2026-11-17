"""STAC APIs (SpatioTemporal Asset Catalog): imagery and elevation catalogues such as Element 84's Earth
Search (Sentinel, Landsat, Copernicus DEM on AWS), Digital Earth Australia and USGS's.

Each collection is a product. A collection holds many items (scenes, or tiles of a grid), so a Dataset
says which: an area (`stac.bbox`), optionally a time (`stac.datetime`), and which of each item's files
(`stac.asset`). `sync` downloads that file from every matching item and mosaics them into one
Cloud-Optimised GeoTIFF, as a raster download's tiles are. The version is a digest of the matching
items and when each last changed, so nothing is downloaded while they stay the same.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import PurePosixPath
from urllib.parse import urlparse

import requests

from . import Download, Listing, NotFound, Product, SourceError, http_session, matches

TIMEOUT = 60
PAGE = 100
MAX_ITEMS = 500  # more files than this is too big an area for one COG: a smaller bbox, or a narrower datetime
MAX_PAGES = 50  # of /collections
GEOJSON = "application/geo+json, application/json;q=0.9"
RASTER_TYPES = ("image/tiff", "image/jp2", "application/x-hdf")
PLANETARY_COMPUTER = "planetarycomputer.microsoft.com"
SIGN = f"https://{PLANETARY_COMPUTER}/api/sas/v1/sign"
AZURE_BLOBS = ".blob.core.windows.net"


def https_href(asset: dict) -> str:
    """An asset's address to download over http(s). Some catalogues give a public S3 bucket's file as
    s3://bucket/key (Earth Search's Copernicus DEM): an https alternate when the asset lists one
    (STAC's alternate-assets extension), else the bucket's own https address, which AWS serves the same file at."""
    href = asset["href"]
    if urlparse(href).scheme in ("http", "https"):
        return href
    for alternate in (asset.get("alternate") or {}).values():
        if isinstance(alternate, dict) and urlparse(alternate.get("href") or "").scheme in ("http", "https"):
            return alternate["href"]
    parsed = urlparse(href)
    if parsed.scheme == "s3" and parsed.netloc:
        return f"https://{parsed.netloc}.s3.amazonaws.com{parsed.path}"
    return href


def is_raster(asset: dict) -> bool:
    kind = (asset.get("type") or "").lower()
    return kind.startswith(RASTER_TYPES) or "data" in (asset.get("roles") or []) and kind.startswith("image/")


def md5_of(checksum) -> str | None:
    """The MD5 in a STAC file:checksum, a multihash: d5 (MD5), 10 (16 bytes), then the digest."""
    return checksum[4:] if isinstance(checksum, str) and checksum.lower().startswith("d510") and len(checksum) == 36 else None


def item_version(item: dict) -> str:
    props = item.get("properties") or {}
    return str(props.get("updated") or props.get("datetime") or props.get("start_datetime") or "")


class STAC:
    name = "STAC API"
    formats = ["COG", "GeoTIFF"]

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        if not url:
            raise SourceError("a stac source needs the STAC API's address: spec.url in the Source, or --url "
                              "(e.g. https://earth-search.aws.element84.com/v1)")
        # A collection's or the search's address still works
        self.url = url.split("?", 1)[0].rstrip("/").split("/collections")[0].removesuffix("/search")
        self.session = session or http_session()

    def _get(self, url: str, what: str, params: dict | None = None, body: dict | None = None):
        try:
            if body is not None:
                r = self.session.post(url, json=body, headers={"Accept": GEOJSON}, timeout=TIMEOUT)
            else:
                r = self.session.get(url, params=params, headers={"Accept": GEOJSON}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SourceError(f"can't reach {url}: {e}") from None
        if r.status_code == 404:
            raise NotFound(what)
        if r.status_code in (401, 403):
            raise SourceError(f"{url} refused the request (HTTP {r.status_code})")
        if r.status_code != 200:
            raise SourceError(f"{url} answered HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise SourceError(f"{url} didn't answer with JSON; is it a STAC API address?") from None
        if not isinstance(data, dict):
            raise SourceError(f"{url} didn't answer with a STAC object")
        return data

    @staticmethod
    def _next(data: dict) -> dict | None:
        return next((link for link in data.get("links") or [] if link.get("rel") == "next" and link.get("href")), None)

    def _product(self, c: dict) -> Product:
        return Product(id=c["id"], name=c.get("title") or c["id"], version=None, formats=self.formats, loads_as="COG",
                       description=c.get("description") or "")

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        found, url = [], f"{self.url}/collections"
        for _ in range(MAX_PAGES):
            data = self._get(url, "collections")
            if "collections" not in data:
                raise SourceError(f"{url} has no collections list; is it a STAC API address?")
            found += [c for c in data["collections"] if isinstance(c, dict) and c.get("id")]
            nxt = self._next(data)
            if not nxt or nxt["href"] == url:
                break
            url = nxt["href"]
        listed = [p for p in (self._product(c) for c in found) if matches(p, search)]
        return Listing(listed[:limit] if limit else listed, len(listed))

    def product(self, collection: str, resource: str | None = None) -> Product:
        """A collection, with the files (assets) each of its items has, as `stac.asset` names them."""
        if resource:
            raise SourceError("a STAC collection has no resources to pick; a Dataset names an asset with stac.asset")
        c = self._get(f"{self.url}/collections/{collection}", collection)
        p = self._product(c)
        assets = c.get("item_assets") or {}
        p.downloads = [Download(format=a.get("type") or "unknown", url="", name=key, file_name=a.get("title") or "",
                                id=key, loadable=is_raster(a)) for key, a in sorted(assets.items())]
        bbox = ((c.get("extent") or {}).get("spatial") or {}).get("bbox") or []
        if bbox:
            p.description = (p.description + f" Covers {', '.join(str(round(x, 3)) for x in bbox[0])}.").strip()
        return p

    def items(self, collection: str, bbox: list[float], datetime: str | None = None) -> list[dict]:
        """The collection's items in `bbox` (and `datetime`), oldest first, so the newest is drawn on top."""
        query = {"collections": collection, "bbox": ",".join(str(x) for x in bbox), "limit": PAGE}
        if datetime:
            query["datetime"] = datetime
        found, url, params, body = [], f"{self.url}/search", query, None
        while True:
            data = self._get(url, collection, params=params, body=body)
            if data.get("type") != "FeatureCollection":
                raise SourceError(f"{self.url}/search didn't answer with items; does the API support item search?")
            found += [f for f in data.get("features") or [] if isinstance(f, dict) and f.get("id")]
            if len(found) > MAX_ITEMS:
                raise SourceError(f"more than {MAX_ITEMS} items of {collection} match: too many files for one raster. "
                                  "Make stac.bbox smaller, or stac.datetime narrower")
            nxt = self._next(data)
            if not nxt:
                break
            url = nxt["href"]
            # A next link either carries its query in the address, or says to POST a body
            body = {**query, "collections": [collection], "bbox": bbox, **(nxt.get("body") or {})} \
                if (nxt.get("method") or "GET").upper() == "POST" else None
            params = None
        return sorted(found, key=lambda f: (item_version(f), f["id"]))

    def selection(self, collection: str, options: dict) -> Product:
        """The collection narrowed to a Dataset's `stac` options: the files to download, and their version."""
        p = self.product(collection)
        key = options["asset"]
        items = self.items(collection, options["bbox"], options.get("datetime"))
        if not items:
            raise SourceError(f"no items of {collection} in {options['bbox']}"
                              + (f" at {options['datetime']}" if options.get("datetime") else ""))
        parts = []
        for i, item in enumerate(items):
            asset = (item.get("assets") or {}).get(key)
            if not asset or not asset.get("href"):
                known = ", ".join(sorted(item.get("assets") or {}))
                raise SourceError(f"item {item['id']} has no asset '{key}' (it has: {known})")
            href = https_href(asset)
            if urlparse(href).scheme not in ("http", "https"):
                raise SourceError(f"item {item['id']}'s '{key}' is at {href}, not an http(s) address dataops can download")
            name = PurePosixPath(urlparse(href).path).name or "asset"
            checksum = asset.get("file:checksum")
            parts.append(Download(format=asset.get("type") or "unknown", url=href, file_name=f"{i:04d}-{name}",
                                  size=asset.get("file:size"), id=item["id"], name=key,
                                  md5=md5_of(checksum)))
        digest = hashlib.sha256(json.dumps([(f["id"], item_version(f)) for f in items] + [key]).encode()).hexdigest()
        p.version = f"{len(items)} items, {digest[:12]}"
        p.downloads, p.chosen, p.loads_as = parts, parts[0], parts[0].format
        p.parts = parts
        return p

    def download_url(self, download: Download) -> str:
        """The file's address. Microsoft's Planetary Computer serves its files from Azure storage that
        refuses plain requests (409): each is signed first, with a token good for about an hour."""
        if urlparse(self.url).hostname != PLANETARY_COMPUTER or not (urlparse(download.url).hostname or "").endswith(AZURE_BLOBS):
            return download.url
        try:
            r = self.session.get(SIGN, params={"href": download.url}, timeout=TIMEOUT)
            href = r.json().get("href") if r.status_code == 200 else None
        except (requests.RequestException, ValueError, AttributeError):
            href = None
        if not href:
            raise SourceError(f"the Planetary Computer wouldn't sign {download.url} for download; try again later")
        return href
