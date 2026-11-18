"""GeoNode: the open-source spatial data platform behind many national, humanitarian and research portals.

The Source's address is the GeoNode (https://stable.demo.geonode.org). Its published datasets are the products,
by their name in GeoNode (`geonode:rutas_espana`, as discover lists them), read through its API (/api/v2).
A dataset is loaded from the exports GeoNode lists for it, made by its own GeoServer: GeoPackage when offered,
else a zipped Shapefile, GeoJSON or GML; a raster dataset's GeoTIFF becomes a Cloud-Optimised GeoTIFF
(`raster:`). `resource` picks another export by name. The version is when the dataset was last updated.
Public datasets only: the original uploaded files need a login.
"""

from __future__ import annotations

import re

import requests

from . import Download, Listing, NotFound, Product, SourceError, http_session
from .catalogue import finish, link_download
from .formats import FILE_LABELS

TIMEOUT = 60
DEFAULT_LIMIT = 20
# GeoNode's export extensions -> the format names the catalogue code ranks
EXPORTS = {"gpkg": "GeoPackage", "zip": "Shapefile", "json": "GeoJSON", "geojson": "GeoJSON", "gml": "GML", "tiff": "GeoTIFF",
           "tif": "GeoTIFF"}


class GeoNode:
    name = "GeoNode"
    formats = FILE_LABELS

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        if not url:
            raise SourceError("a geonode source needs the GeoNode's address: spec.url in the Source, or --url "
                              "(e.g. https://stable.demo.geonode.org)")
        self.url = re.sub(r"/(api(/v2)?/?.*)?$", "", url.rstrip("/"))
        self.session = session or http_session()

    def _datasets(self, params: dict, what: str) -> dict:
        try:
            r = self.session.get(f"{self.url}/api/v2/datasets", params={"filter{is_published}": "true", **params},
                                 headers={"Accept": "application/json"}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SourceError(f"can't reach {self.url}: {e}") from None
        if r.status_code != 200:
            raise SourceError(f"{self.url} answered HTTP {r.status_code} for {what}; is it a GeoNode (3.2 or later)?")
        try:
            return r.json()
        except ValueError:
            raise SourceError(f"{self.url} didn't answer with JSON; is it a GeoNode (3.2 or later)?") from None

    def _product(self, dataset: dict) -> Product:
        downloads = []
        name = str(dataset.get("alternate") or dataset.get("name"))
        for link in dataset.get("links") or []:
            if not isinstance(link, dict) or link.get("link_type") != "data" or not link.get("url"):
                continue
            if str(link.get("name", "")).lower() == "original":  # the uploaded file: needs a login
                continue
            label = EXPORTS.get(str(link.get("extension") or "").lower())
            d = link_download(link["url"], label, link.get("name") or label or "", link.get("name") or link["url"])
            if label:
                ext = {"Shapefile": ".zip", "GeoPackage": ".gpkg", "GeoJSON": ".geojson", "GML": ".gml", "GeoTIFF": ".tif"}[label]
                d.format, d.loadable = label, True
                d.file_name = f"{name.split(':')[-1]}{ext}"  # the export's address has no file name: GDAL needs the extension
            downloads.append(d)
        return Product(id=name, name=str(dataset.get("title") or name), version=dataset.get("last_updated") or dataset.get("date"),
                       downloads=downloads, description=re.sub(r"<[^>]+>", " ", str(dataset.get("abstract") or ""))[:300])

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        params = {"page_size": limit or DEFAULT_LIMIT}
        if search:
            params["search"] = search
        data = self._datasets(params, "the datasets")
        found = [finish(self._product(d), None) for d in data.get("datasets") or [] if isinstance(d, dict)]
        return Listing(found, data.get("total"))

    def product(self, name: str, resource: str | None = None) -> Product:
        key = "filter{pk}" if name.isdigit() else "filter{alternate}"
        found = [d for d in self._datasets({key: name}, name).get("datasets") or [] if isinstance(d, dict)]
        if not found and ":" not in name:  # the name without its workspace
            found = [d for d in self._datasets({"filter{alternate}": f"geonode:{name}"}, name).get("datasets") or []]
        if not found:
            raise NotFound(name)
        return finish(self._product(found[0]), resource)

    def download_url(self, download: Download) -> str:
        return download.url
