"""data.europa.eu: the EU's open data portal, which gathers the catalogues of every national, regional and city
portal in Europe (about 1.6 million datasets) behind one search API.

A dataset is a product, by its ID on data.europa.eu (`pdcn_isos_p-canton-geneve`, as discover lists it). Its
distributions are the downloads; the best file is loaded as a catalogue record's is (GeoPackage first), and
`resource` picks another. A distribution that is a service (WFS, ArcGIS REST) is listed with how to discover
it. The version is when the dataset last changed. `discover` lists datasets with a geospatial file.
"""

from __future__ import annotations

import requests

from . import Listing, NotFound, Product, SourceError, http_session
from .catalogue import finish, link_download
from .formats import FILE_LABELS

API = "https://data.europa.eu/api/hub/search"
TIMEOUT = 60
DEFAULT_LIMIT = 20
GEO_FORMATS = ["GPKG", "SHP", "GEOJSON", "GML", "KML", "FGB", "GDB"]  # the search lists datasets with one of these


def text(value) -> str:
    """A multilingual field ({"en": ..., "de": ...}): English, else the first language."""
    if isinstance(value, dict):
        return str(value.get("en") or next(iter(value.values()), "") or "")
    return str(value or "")


def fmt(distribution: dict) -> str:
    """A distribution's format, as the catalogue code reads it: WFS_SRVC is a WFS."""
    raw = str((distribution.get("format") or {}).get("id") or "")
    return raw.removesuffix("_SRVC")


class DataEuropa:
    name = "data.europa.eu"
    formats = FILE_LABELS

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        self.url = (url or API).rstrip("/")
        self.session = session or http_session()

    def _get(self, path: str, what: str, params: dict | None = None) -> dict:
        try:
            r = self.session.get(f"{self.url}/{path}", params=params, headers={"Accept": "application/json"}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SourceError(f"can't reach {self.url}: {e}") from None
        if r.status_code == 404:
            raise NotFound(what)
        if r.status_code != 200:
            raise SourceError(f"{self.url} answered HTTP {r.status_code} for {what}")
        try:
            data = r.json()
        except ValueError:
            raise SourceError(f"{self.url} didn't answer with JSON") from None
        return data.get("result", data) if isinstance(data, dict) else {}

    def _product(self, dataset: dict) -> Product:
        downloads = []
        for d in dataset.get("distributions") or []:
            href = next(iter(d.get("download_url") or []), None) or next(iter(d.get("access_url") or []), None)
            if not href:
                continue
            title = text(d.get("title"))
            if not title or title.isdigit():  # some publishers title them 0, 1...: the description says more
                title = text(d.get("description"))[:80] or fmt(d)
            downloads.append(link_download(href, fmt(d), title, str(d.get("id") or href)))
        modified = dataset.get("modified") or max((str(d.get("modified")) for d in dataset.get("distributions") or []
                                                   if d.get("modified")), default=None)
        return Product(id=str(dataset["id"]), name=text(dataset.get("title")) or str(dataset["id"]), version=modified,
                       downloads=downloads, description=text(dataset.get("description"))[:300])

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        params = {"filter": "dataset", "limit": limit or DEFAULT_LIMIT, "facets": '{"format": %s}' % str(GEO_FORMATS).replace("'", '"')}
        if search:
            params["q"] = search
        result = self._get("search", "the search", params)
        found = [finish(self._product(r), None) for r in result.get("results") or [] if isinstance(r, dict) and r.get("id")]
        return Listing(found, result.get("count"))

    def product(self, dataset_id: str, resource: str | None = None) -> Product:
        return finish(self._product(self._get(f"datasets/{dataset_id}", dataset_id)), resource)

    def download_url(self, download) -> str:
        return download.url
