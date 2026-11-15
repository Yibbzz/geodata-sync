"""Open data portal platforms: Socrata (most US city and state portals: New York, Chicago, Seattle, ...) and
OpenDataSoft (common in France, Belgium and Switzerland, and data.opendatasoft.com).

Each portal's datasets with geometry are its products, found by search. A product is exported whole as
GeoJSON and loaded as a downloaded file. The version is when its data last changed, so an unchanged
dataset isn't exported again.
"""

from __future__ import annotations

from urllib.parse import quote, urlparse

import requests

from . import Download, Listing, NotFound, Product, SourceError, http_session

TIMEOUT = 60
DEFAULT_LIMIT = 20
GEOJSON = "GeoJSON"


class _Portal:
    formats = [GEOJSON]

    def __init__(self, url: str | None, kind: str, example: str, session: requests.Session | None):
        if not url:
            raise SourceError(f"a {kind} source needs the portal's address: spec.url in the Source, or --url (e.g. {example})")
        parts = urlparse(url if "://" in url else f"https://{url}")
        self.portal = f"{parts.scheme}://{parts.netloc}"
        self.session = session or http_session()

    def _json(self, url: str, what: str, params: dict | None = None):
        try:
            r = self.session.get(url, params=params, headers={"Accept": "application/json"}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SourceError(f"can't reach {self.portal}: {e}") from None
        if r.status_code == 404:
            raise NotFound(what)
        if r.status_code in (401, 403):
            raise SourceError(f"{self.portal} refused the request (HTTP {r.status_code}); dataops reads public datasets only")
        if r.status_code != 200:
            raise SourceError(f"{self.portal} answered HTTP {r.status_code} for {url}")
        try:
            return r.json()
        except ValueError:
            raise SourceError(f"{url} didn't answer with JSON; is {self.portal} a {self.name}?") from None

    def download_url(self, download: Download) -> str:
        return download.url  # an export made on request: asked for once, when it is downloaded


# --- Socrata

# Column types that hold geometry
SOCRATA_GEOMETRY = {"point", "multipoint", "line", "multiline", "polygon", "multipolygon", "location"}


class Socrata(_Portal):
    name = "Socrata portal"

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        super().__init__(url, "socrata", "https://data.cityofnewyork.us", session)
        self.app_token = api_key  # optional: Socrata throttles requests without one

    def _json(self, url, what, params=None):
        if self.app_token:
            self.session.headers["X-App-Token"] = self.app_token
        return super()._json(url, what, params)

    @staticmethod
    def spatial(resource: dict) -> bool:
        if resource.get("type") == "map":
            return True
        return any(str(t).lower() in SOCRATA_GEOMETRY for t in resource.get("columns_datatype") or [])

    def _product(self, resource: dict) -> Product:
        rid = resource["id"]
        if resource.get("type") == "map":  # a geospatial asset (an uploaded shapefile, say): its export
            url = f"{self.portal}/api/geospatial/{rid}?method=export&format=GeoJSON"
        else:  # a table with a geometry column: every row, as GeoJSON
            url = f"{self.portal}/resource/{rid}.geojson?$limit=1000000000&$order=:id"
        download = Download(format=GEOJSON, url=url, file_name=f"{rid}.geojson", id=rid, name=resource.get("name") or rid)
        return Product(
            id=rid, name=resource.get("name") or rid,
            version=resource.get("data_updated_at") or resource.get("updatedAt"),
            formats=[GEOJSON], downloads=[download], loads_as=GEOJSON, chosen=download,
            description=resource.get("description") or "")

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        params = {"domains": urlparse(self.portal).netloc, "only": "dataset,map", "limit": limit or DEFAULT_LIMIT}
        if search:
            params["q"] = search
        data = self._json(f"{self.portal}/api/catalog/v1", "catalog", params)
        resources = [r.get("resource") or {} for r in data.get("results") or []]
        found = [self._product(r) for r in resources if r.get("id") and self.spatial(r)]
        return Listing(found, data.get("resultSetSize"))

    def product(self, dataset_id: str, resource: str | None = None) -> Product:
        if resource:
            raise SourceError("a Socrata dataset is exported whole; there are no resources to pick")
        data = self._json(f"{self.portal}/api/catalog/v1", dataset_id, {"ids": dataset_id})
        results = [r.get("resource") or {} for r in data.get("results") or []]
        found = next((r for r in results if r.get("id") == dataset_id), None)
        if found is None:
            raise NotFound(dataset_id)
        if not self.spatial(found):
            raise SourceError(f"{dataset_id} has no geometry column, so there is nothing dataops can load")
        return self._product(found)


# --- OpenDataSoft


class OpenDataSoft(_Portal):
    name = "OpenDataSoft portal"
    API = "/api/explore/v2.1"

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        super().__init__(url, "opendatasoft", "https://data.opendatasoft.com", session)
        if api_key:
            self.session.headers["Authorization"] = f"Apikey {api_key}"  # sent only to the portal

    def _product(self, d: dict) -> Product:
        meta = ((d.get("metas") or {}).get("default") or {})
        did = d["dataset_id"]
        download = Download(format=GEOJSON, url=f"{self.portal}{self.API}/catalog/datasets/{quote(did)}/exports/geojson",
                            file_name=f"{did}.geojson", id=did, name=meta.get("title") or did)
        return Product(
            id=did, name=meta.get("title") or did, version=meta.get("data_processed") or meta.get("modified"),
            formats=[GEOJSON], downloads=[download], loads_as=GEOJSON, chosen=download,
            description=meta.get("description") or "")

    @staticmethod
    def spatial(d: dict) -> bool:
        return "geo" in (d.get("features") or [])

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        where = ['features = "geo"']
        if search:
            where.append(" and ".join(f'search("{w}")' for w in search.replace('"', "").split()))
        data = self._json(f"{self.portal}{self.API}/catalog/datasets", "catalog",
                          {"where": " and ".join(where), "limit": limit or DEFAULT_LIMIT})
        found = [self._product(d) for d in data.get("results") or [] if d.get("dataset_id")]
        return Listing(found, data.get("total_count"))

    def product(self, dataset_id: str, resource: str | None = None) -> Product:
        if resource:
            raise SourceError("an OpenDataSoft dataset is exported whole; there are no resources to pick")
        d = self._json(f"{self.portal}{self.API}/catalog/datasets/{quote(dataset_id)}", dataset_id)
        if not self.spatial(d):
            raise SourceError(f"{dataset_id} has no geometry (no geo feature), so there is nothing dataops can load")
        return self._product(d)
