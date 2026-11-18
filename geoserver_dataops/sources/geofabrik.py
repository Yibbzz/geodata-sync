"""OpenStreetMap extracts from Geofabrik (download.geofabrik.de): every continent, country and many regions,
rebuilt daily.

Each region in Geofabrik's index is a product (`great-britain`, `bayern`, `new-zealand`). Where Geofabrik
makes its "free" shapefile pack, about 20 tidy layers (roads, buildings, places of interest, land use, water),
that is loaded; for a large region it doesn't make one, and the raw OSM extract (.osm.pbf) is loaded through
GDAL's OSM driver instead (layers points, lines, multilinestrings, multipolygons, other_relations).

The version is when Geofabrik last built the file, asked with a one-byte request, so the file is only
downloaded once it has changed; it is checked against the MD5 Geofabrik publishes beside it.
"""

from __future__ import annotations

import requests

from . import Download, Listing, NotFound, Product, SourceError, http_session, matches
from .url import probe

BASE = "https://download.geofabrik.de"
TIMEOUT = 60
SHAPEFILES, OSM = "Shapefile", "OSM PBF"


class Geofabrik:
    name = "Geofabrik"
    formats = [SHAPEFILES, OSM]

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        self.base = (url or BASE).rstrip("/")  # a mirror, if given
        self.session = session or http_session()
        self._index: list[dict] | None = None

    def index(self) -> list[dict]:
        """The regions: Geofabrik's index without the shapes (index-v1-nogeom.json)."""
        if self._index is None:
            url = f"{self.base}/index-v1-nogeom.json"
            try:
                r = self.session.get(url, timeout=TIMEOUT)
            except requests.RequestException as e:
                raise SourceError(f"can't reach {self.base}: {e}") from None
            if r.status_code != 200:
                raise SourceError(f"{url} answered HTTP {r.status_code}")
            try:
                features = r.json().get("features") or []
            except (ValueError, AttributeError):
                raise SourceError(f"{url} isn't Geofabrik's index") from None
            self._index = [f.get("properties") or {} for f in features if isinstance(f, dict)]
        return self._index

    def _product(self, region: dict, details: bool = False) -> Product:
        urls = region.get("urls") or {}
        downloads = []
        if urls.get("shp"):
            downloads.append(Download(format=SHAPEFILES, url=urls["shp"], file_name=urls["shp"].rsplit("/", 1)[-1],
                                      name="free shapefiles", id="shp"))
        if urls.get("pbf"):
            downloads.append(Download(format=OSM, url=urls["pbf"], file_name=urls["pbf"].rsplit("/", 1)[-1],
                                      name="OSM extract", id="pbf"))
        parent = next((r.get("name") for r in self.index() if r.get("id") == region.get("parent")), None)
        p = Product(id=region["id"], name=region.get("name") or region["id"], version=None,
                    formats=[d.format for d in downloads], downloads=downloads,
                    loads_as=downloads[0].format if downloads else None,
                    description=f"OpenStreetMap, {region.get('name')}" + (f" ({parent})" if parent else ""))
        return p

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        found = [p for p in (self._product(r) for r in self.index() if r.get("id")) if matches(p, search)]
        found.sort(key=lambda p: p.id)
        return Listing(found[:limit] if limit else found, len(found))

    def product(self, region_id: str, resource: str | None = None) -> Product:
        region = next((r for r in self.index() if r.get("id") == region_id), None)
        if region is None:
            raise NotFound(region_id)
        p = self._product(region)
        if not p.downloads:
            raise SourceError(f"Geofabrik has no extract of {region_id} to download")
        if resource:
            picked = [d for d in p.downloads if resource.lower() in (d.id, d.format.lower(), d.name)]
            if not picked:
                raise SourceError(f"{region_id} has no '{resource}': pick shp (the free shapefiles) or pbf (the OSM extract)")
            p.chosen = picked[0]
        else:
            p.chosen = p.downloads[0]  # the shapefiles where Geofabrik makes them: tidier layers than raw OSM
        p.loads_as = p.chosen.format
        found = probe(self.session, p.chosen.url)  # when it was built: one byte asked for
        # when Geofabrik built the file (shown as its date); the ETag only if the server gives no time
        p.version, p.chosen.size = found.modified or found.version, found.size
        p.chosen.md5 = self._md5(p.chosen.url)
        return p

    def _md5(self, url: str) -> str | None:
        """The MD5 Geofabrik publishes beside each file (<file>.md5: '<digest>  <name>')."""
        try:
            r = self.session.get(f"{url}.md5", timeout=TIMEOUT)
        except requests.RequestException:
            return None
        text = r.text.split() if r.status_code == 200 else []
        return text[0] if text and len(text[0]) == 32 else None

    def download_url(self, download: Download) -> str:
        return download.url
