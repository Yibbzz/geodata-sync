"""OpenStreetMap features picked by their tags in an area, from an Overpass API (overpass-api.de, or
another instance or a private one).

A product is an Overpass selector: `nwr[amenity=pub]` (every node, way and relation tagged as a pub),
`way[highway]`, or several separated by `;`. A Dataset names its area with `overpass.bbox`; dataops
asks for those features with the nodes and ways that make up their shapes, as OSM XML, and loads them
through GDAL's OSM reader: points, lines, multilinestrings, multipolygons and other_relations.
Only the layers that have features are loaded, so `nwr[amenity=pub]` gives points and multipolygons.

Overpass has no version: what it sends is the version, so a Dataset is loaded again only when the
features changed. It stamps each answer with the time of its data, which is left out of the file.
Overpass is shared: keep areas small and updates daily or slower.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import requests

from . import Download, Listing, Product, SourceError, http_session
from .. import __version__
from ..remote import GDAL_ENV

BASE = "https://overpass-api.de/api/interpreter"
TIMEOUT = 300  # seconds: Overpass is often busy, and a large area takes minutes
FORMAT = "OSM XML"
OSM_LAYERS = ("points", "lines", "multilinestrings", "multipolygons", "other_relations")
STAMP = re.compile(rb'\s*<meta osm_base="[^"]*"( areas="[^"]*")?\s*/>')  # the time of Overpass's data, in every answer
STATEMENT = re.compile(r"^\s*(node|way|rel|relation|nwr|nw|nr|wr)(\[[^\]]+\])+\s*$")


def statements(selector: str) -> list[str]:
    """The selector's statements: `nwr[amenity=pub]; way[highway]`. Only element type and tag filters, so a
    product can't be a whole query of its own (output, recursion, another area)."""
    found = [s.strip() for s in selector.split(";") if s.strip()]
    if not found or not all(STATEMENT.match(s) for s in found):
        raise SourceError(f"'{selector}' isn't an Overpass selector: give element types and tag filters, such as "
                          "nwr[amenity=pub] or way[highway], several separated by ;")
    return found


def query(selector: str, bbox: list[float]) -> str:
    west, south, east, north = bbox
    if west >= east or south >= north:
        raise SourceError(f"overpass.bbox {bbox} is empty: give [west, south, east, north], in degrees")
    area = f"({south},{west},{north},{east})"
    return (f"[out:xml][timeout:{TIMEOUT - 30}];("
            + "".join(f"{s}{area};" for s in statements(selector))
            + ");(._;>;);out body;")


class Overpass:
    name = "Overpass API"
    formats = [FORMAT]

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        self.url = (url or BASE).rstrip("/")
        self.session = session or http_session()
        # Overpass refuses clients that don't say who they are (HTTP 406)
        self.session.headers["User-Agent"] = f"geoserver-dataops/{__version__}"

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        raise SourceError("an overpass source has no list of products; the product is what to load, as an Overpass "
                          "selector such as nwr[amenity=pub] or way[highway], and overpass.bbox its area")

    def product(self, selector: str, resource: str | None = None) -> Product:
        if resource:
            raise SourceError("an Overpass selector has no resources to pick; a Dataset names its area with overpass.bbox")
        statements(selector)
        download = Download(format=FORMAT, url=self.url, file_name="overpass.gpkg", id=selector, name=selector)
        return Product(id=selector, name=selector, version=None, formats=[FORMAT], downloads=[download],
                       loads_as=FORMAT, chosen=download, description=f"OpenStreetMap features from {self.url}")

    def selection(self, selector: str, options: dict) -> Product:
        p = self.product(selector)
        p.chosen.area = query(selector, options["bbox"])
        return p

    def download_url(self, download: Download) -> str:
        return download.url

    def fetch(self, download: Download, dest: Path) -> str:
        """Ask Overpass for the features, and write the layers that have any to the GeoPackage `dest`.
        Returns the SHA-256 of the OSM data, without Overpass's time stamp: the same features, the same digest."""
        try:
            r = self.session.post(self.url, data={"data": download.area}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SourceError(f"can't reach {self.url}: {e}") from None
        if r.status_code in (429, 502, 503, 504):  # the public servers are often full: "at capacity"
            raise SourceError(f"{self.url} is busy (HTTP {r.status_code}); the next run tries again, or use another "
                              "Overpass instance (the Source's url)")
        if r.status_code != 200:
            detail = " ".join(re.sub(r"<[^>]+>", " ", r.text).split())[:200]
            raise SourceError(f"{self.url} answered HTTP {r.status_code}: {detail or 'no details'}")
        if b"<osm" not in r.content[:1000]:
            raise SourceError(f"{self.url} didn't answer with OSM data")
        if b'<remark> runtime error' in r.content:
            remark = re.search(rb"<remark>(.*?)</remark>", r.content, re.S)
            raise SourceError(f"Overpass stopped: {remark.group(1).decode(errors='replace').strip() if remark else 'runtime error'}; "
                              "a smaller area, or a narrower selector")
        data = STAMP.sub(b"", r.content, count=1)
        osm = dest.with_suffix(".osm")
        osm.write_bytes(data)
        to_geopackage(osm, dest)
        return hashlib.sha256(data).hexdigest()


def to_geopackage(osm: Path, dest: Path) -> None:
    """The OSM file's layers that have features, in a GeoPackage. GDAL's OSM reader always offers all five."""
    if shutil.which("ogr2ogr") is None:
        raise SourceError("loading OpenStreetMap data needs GDAL's ogr2ogr on the PATH")
    env = {**GDAL_ENV, **os.environ}
    # All layers in one pass (the OSM reader can't count a layer without reading the file), then the
    # empty ones dropped from the GeoPackage, where counting is cheap
    done = subprocess.run(["ogr2ogr", "-f", "GPKG", "-if", "OSM", str(dest), str(osm), *OSM_LAYERS],
                          capture_output=True, text=True, env=env)
    if done.returncode != 0:
        raise SourceError(f"GDAL couldn't read the OSM data: {done.stderr.strip()[-200:]}")
    info = subprocess.run(["ogrinfo", "-ro", "-so", "-json", str(dest)], capture_output=True, text=True, env=env)
    try:
        counts = {layer["name"]: layer.get("featureCount") or 0 for layer in json.loads(info.stdout)["layers"]}
    except (ValueError, KeyError):
        raise SourceError("GDAL couldn't read back the OpenStreetMap layers it wrote") from None
    if not any(counts.values()):
        raise SourceError("Overpass found no features for this selector in this area")
    for layer in (name for name, count in counts.items() if count == 0):
        subprocess.run(["ogrinfo", str(dest), "-sql", f'DROP TABLE "{layer}"'], capture_output=True, text=True, env=env)
