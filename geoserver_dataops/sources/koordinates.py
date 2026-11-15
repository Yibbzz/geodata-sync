"""Koordinates data services: New Zealand's LINZ Data Service (data.linz.govt.nz), Stats NZ's Datafinder, the
Ministry for the Environment's, and others on koordinates.com.

Each vector layer or table is a product, found by search. Koordinates needs an API key for everything
(credentials: { secret: ... }, key apiKey), sent in the Authorization header to the portal only.

Data is had through an export: sync asks for one (a GeoPackage, in the layer's own CRS), waits while
Koordinates makes it, then downloads it. The version is the layer's current version, so nothing is exported
while it stays the same.
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlparse

import requests

from .. import ingest
from . import Download, Listing, NotFound, Product, SourceError, http_session

API = "/services/api/v1.x"
TIMEOUT = 60
DEFAULT_LIMIT = 20
GPKG = "application/x-ogc-gpkg"
EXPORT_WAIT = 3600  # seconds an export may take to make; a whole-country layer can take a while
POLL = 10


class Koordinates:
    name = "Koordinates data service"
    formats = ["GeoPackage"]

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        if not url:
            raise SourceError("a koordinates source needs the portal's address: spec.url in the Source, or --url "
                              "(e.g. https://data.linz.govt.nz)")
        if not api_key:
            raise SourceError("a koordinates source needs an API key (made on the portal, under API keys): "
                              "credentials: { secret: <name> } with key apiKey")
        parts = urlparse(url if "://" in url else f"https://{url}")
        self.portal = f"{parts.scheme}://{parts.netloc}"
        self.api = self.portal + API
        self.session = session or http_session()
        self.session.headers["Authorization"] = f"key {api_key}"  # this session only talks to the portal

    def _request(self, method: str, url: str, what: str, **kwargs) -> requests.Response:
        try:
            r = self.session.request(method, url, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as e:
            raise SourceError(f"can't reach {self.portal}: {e}") from None
        if r.status_code == 404:
            raise NotFound(what)
        if r.status_code in (401, 403):
            raise SourceError(f"{self.portal} refused the request (HTTP {r.status_code}); check the API key, "
                              "and that it may export (its 'Full access to exports' scope)")
        if r.status_code not in (200, 201, 202, 302, 303):
            raise SourceError(f"{self.portal} answered HTTP {r.status_code} for {url}")
        return r

    def _json(self, method: str, url: str, what: str, **kwargs):
        r = self._request(method, url, what, **kwargs)
        try:
            return r.json()
        except ValueError:
            raise SourceError(f"{url} didn't answer with JSON; is {self.portal} a Koordinates portal?") from None

    def _product(self, layer: dict) -> Product:
        lid = str(layer["id"])
        version = layer.get("version") or {}
        crs = ((layer.get("data") or {}).get("crs") or {}).get("id")
        download = Download(format="GeoPackage", url=f"{self.api}/layers/{lid}/", file_name=f"layer-{lid}.zip",
                            id=lid, name=layer.get("title") or lid, area=crs or "")
        return Product(
            id=lid, name=layer.get("title") or lid,
            version=str(version.get("id")) if isinstance(version, dict) and version.get("id") else layer.get("published_at"),
            formats=["GeoPackage"], downloads=[download], loads_as="GeoPackage", chosen=download,
            description=layer.get("description") or "")

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        params = {"page_size": limit or DEFAULT_LIMIT, "kind": "vector,table"}
        if search:
            params["q"] = search
        r = self._request("GET", f"{self.api}/layers/", "layers", params=params)
        try:
            layers = r.json()
        except ValueError:
            raise SourceError(f"{self.api}/layers/ didn't answer with JSON; is {self.portal} a Koordinates portal?") from None
        total = r.headers.get("X-Resource-Range", "").rsplit("/", 1)[-1]
        found = [self._product(layer) for layer in layers if isinstance(layer, dict) and layer.get("id")]
        return Listing(found, int(total) if total.isdigit() else None)

    def product(self, layer_id: str, resource: str | None = None) -> Product:
        if resource:
            raise SourceError("a Koordinates layer is exported whole; there are no resources to pick")
        layer = self._json("GET", f"{self.api}/layers/{layer_id}/", layer_id)
        kind = layer.get("kind")
        if kind not in ("vector", "table"):
            raise SourceError(f"layer {layer_id} is a {kind or 'layer'}; dataops loads Koordinates' vector layers and tables")
        return self._product(layer)

    def download_url(self, download: Download) -> str:
        """Ask for an export of the layer, wait while Koordinates makes it, and give where to download it."""
        body = {"items": [{"item": download.url}], "formats": {"vector": GPKG, "table": GPKG}}
        if download.area:
            body["crs"] = download.area  # the layer's own CRS: nothing reprojected
        export = self._json("POST", f"{self.api}/exports/", download.id, json=body)
        deadline = time.monotonic() + EXPORT_WAIT
        while export.get("state") not in ("complete", "error", "cancelled", "gone"):
            if time.monotonic() > deadline:
                raise SourceError(f"the export of layer {download.id} wasn't ready after {EXPORT_WAIT // 60} minutes")
            time.sleep(POLL)
            export = self._json("GET", export.get("url") or f"{self.api}/exports/{export['id']}/", download.id)
        if export.get("state") != "complete" or not export.get("download_url"):
            raise SourceError(f"Koordinates couldn't export layer {download.id} ({export.get('state')})")
        # The download link answers with a redirect to signed storage, which needs no key
        r = self._request("GET", export["download_url"], download.id, allow_redirects=False, stream=True)
        r.close()
        return r.headers.get("Location") or export["download_url"]

    def fetch(self, download: Download, dest: Path) -> str:
        """Export the layer and download it to `dest`; returns its SHA-256. The key goes with the download only
        when the file is on the portal itself, never to the storage an export may be redirected to."""
        url = self.download_url(download)
        on_portal = urlparse(url).netloc == urlparse(self.portal).netloc
        return ingest.download(url, dest, session=self.session if on_portal else None)
