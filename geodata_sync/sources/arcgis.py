"""ArcGIS REST services (ArcGIS Server and ArcGIS Online). Each layer of a FeatureServer or MapServer is a product.

The Source's address is a services directory (…/arcgis/rest/services), a folder in it, one service
(…/FeatureServer) or one layer (…/FeatureServer/0). A product's ID is its path below the directory:
`Hydrography/Rivers/FeatureServer/0`. Its version is the layer's last data edit, which most hosted and
many server layers report, so an unchanged layer isn't loaded again; one that reports none is loaded
each time it is due, as a WFS is.

GDAL's ESRIJSON driver loads a layer from its query endpoint and pages through it itself, ordered by
the object ID so no feature is read twice or missed.
"""

from __future__ import annotations

import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

import requests

from .. import remote
from . import Download, Listing, NotFound, Product, SourceError, http_session, matches

TIMEOUT = 60
FORMAT = "ArcGIS REST"
SERVICE_KINDS = ("FeatureServer", "MapServer")
MAX_FOLDERS = 200  # a directory with more is listed only this far: name a folder in the Source's address
MAX_SERVICES = 500

# Esri geometry types as GDAL names them
GEOMETRY = {"esriGeometryPoint": "Point", "esriGeometryMultipoint": "MultiPoint", "esriGeometryPolyline": "MultiLineString",
            "esriGeometryPolygon": "MultiPolygon", "esriGeometryEnvelope": "Polygon"}
# Esri's own codes for EPSG systems
ESRI_WKIDS = {102100: 3857, 102113: 3857}

SERVICE_PATH = re.compile(r"^(?P<service>.+?/(?:FeatureServer|MapServer))(?:/(?P<layer>\d+))?(?:/.*)?$")


def split(url: str) -> tuple[str, str]:
    """(services directory, path below it) from any address inside an ArcGIS REST API."""
    url = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    marker = "/rest/services"
    at = url.lower().find(marker)
    if at < 0:
        raise SourceError(f"{url} isn't an ArcGIS REST address: it has no /rest/services in it, "
                          "e.g. https://services.arcgis.com/<org>/arcgis/rest/services")
    path = url[at + len(marker):].strip("/")
    m = SERVICE_PATH.match(path)
    if m:  # …/FeatureServer/0/query and the like: the layer, or the service
        path = m.group("service") + (f"/{m.group('layer')}" if m.group("layer") is not None else "")
    return url[: at + len(marker)], path


def epsg(spatial_reference: dict | None) -> str | None:
    sr = spatial_reference or {}
    wkid = sr.get("latestWkid") or sr.get("wkid")
    if not isinstance(wkid, int):
        return None
    return f"EPSG:{ESRI_WKIDS.get(wkid, wkid)}"


def edited(info: dict) -> str | None:
    """The layer's last data edit, as an ISO timestamp: its version."""
    editing = info.get("editingInfo") or {}
    ms = editing.get("dataLastEditDate") or editing.get("lastEditDate")
    if not isinstance(ms, (int, float)) or ms <= 0:
        return None
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class ArcGIS:
    name = "ArcGIS REST"
    formats = [FORMAT]

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        if not url:
            raise SourceError("an arcgis source needs the services directory's address: spec.url in the Source, or --url "
                              "(e.g. https://services.arcgis.com/<org>/arcgis/rest/services)")
        self.root, self.scope = split(url)
        self.url = f"{self.root}/{self.scope}".rstrip("/")
        self.session = session or http_session()
        self._info: dict[str, dict] = {}

    def _json(self, path: str, what: str, **params) -> dict:
        url = f"{self.root}/{path}".rstrip("/")
        try:
            r = self.session.get(url, params={"f": "json", **params}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SourceError(f"can't reach {url}: {e}") from None
        if r.status_code == 404:
            raise NotFound(what)
        if r.status_code in (401, 403):
            raise SourceError(f"{url} refused the request (HTTP {r.status_code}); geodata reads public services only")
        if r.status_code != 200:
            raise SourceError(f"{url} answered HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            raise SourceError(f"{url} didn't answer with JSON; is it an ArcGIS REST address?") from None
        error = data.get("error") if isinstance(data, dict) else None
        if error:  # ArcGIS answers 200 with an error object
            code, message = error.get("code"), str(error.get("message") or "")
            # a missing service is "not found"; a layer ID the service hasn't, "invalid or missing input parameters"
            if code == 404 or (code == 400 and any(m in message.lower() for m in ("not found", "invalid or missing input"))):
                raise NotFound(what)
            if code in (498, 499):
                raise SourceError(f"{url} needs a token; geodata reads public services only")
            raise SourceError(f"{url}: {message or 'error'} (code {code})")
        if not isinstance(data, dict):
            raise SourceError(f"{url} didn't answer with an ArcGIS JSON object")
        return data

    # --- listing

    def _services(self) -> list[str]:
        """Paths of the FeatureServer and MapServer services in scope (a MapServer is left out when a
        FeatureServer of the same name is there: the same data)."""
        if SERVICE_PATH.match(self.scope):
            return [SERVICE_PATH.match(self.scope).group("service")]
        found, folders, seen = [], [self.scope], 0
        while folders and seen < MAX_FOLDERS and len(found) < MAX_SERVICES:
            folder = folders.pop(0)
            seen += 1
            data = self._json(folder, folder or "services directory")
            folders += [f if not folder or f.startswith(folder + "/") else f"{folder}/{f}"
                        for f in data.get("folders") or [] if isinstance(f, str)]
            for s in data.get("services") or []:
                if isinstance(s, dict) and s.get("type") in SERVICE_KINDS and s.get("name"):
                    found.append(f"{s['name']}/{s['type']}")
        features = {s.rsplit("/", 1)[0] for s in found if s.endswith("/FeatureServer")}
        return sorted(s for s in found if s.endswith("/FeatureServer") or s.rsplit("/", 1)[0] not in features)

    def _layers_of(self, service: str) -> list[Product]:
        try:
            data = self._json(service, service)
        except SourceError as e:  # a secured service, or one that is down: the others are still worth listing
            print(f"geodata: note: {service} left out: {e}", file=sys.stderr)
            return []
        products = []
        for layer in (data.get("layers") or []) + (data.get("tables") or []):
            if not isinstance(layer, dict) or not isinstance(layer.get("id"), int) or layer.get("subLayerIds"):
                continue  # a group layer holds others, and no features of its own
            products.append(Product(
                id=f"{service}/{layer['id']}", name=layer.get("name") or str(layer["id"]), version=None,
                formats=[FORMAT], loads_as=FORMAT, description=service.rsplit("/", 1)[0]))
        return products

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        m = SERVICE_PATH.match(self.scope)
        if m and m.group("layer") is not None:
            p = self.product(self.scope)
            return Listing([p], 1)
        services = self._services()
        with ThreadPoolExecutor(max_workers=8) as pool:
            layers = [p for ps in pool.map(self._layers_of, services) for p in ps]
        found = [p for p in layers if matches(p, search)]
        return Listing(found[:limit] if limit else found, len(found))

    # --- one layer

    def product(self, layer_path: str, resource: str | None = None) -> Product:
        if resource:
            raise SourceError("an ArcGIS layer is one layer; there are no resources to pick")
        path = layer_path.strip("/")
        if "/rest/services" in path.lower():  # a whole address, as copied from a browser
            root, path = split(path)
            if root.lower() != self.root.lower():
                raise SourceError(f"{layer_path} isn't in this source's services directory ({self.root})")
        m = SERVICE_PATH.match(path)
        if not m or m.group("layer") is None:
            raise SourceError(f"'{layer_path}' isn't a layer: give its path below the services directory, "
                              "e.g. Hydrography/Rivers/FeatureServer/0 (`geodata discover` lists them)")
        path = f"{m.group('service')}/{m.group('layer')}"
        info = self._json(path, layer_path)
        if info.get("type") not in ("Feature Layer", "Table"):
            raise SourceError(f"{path} is a {info.get('type') or 'layer'} geodata can't load (only feature layers and tables)")
        if "query" not in str(info.get("capabilities", "Query")).lower():
            raise SourceError(f"{path} can't be queried, so its features can't be read")
        self._info[path] = info
        download = Download(format=FORMAT, url=f"{self.root}/{path}", name=info.get("name") or path, service=True)
        return Product(
            id=path, name=info.get("name") or path, version=edited(info), formats=[FORMAT], loads_as=FORMAT,
            downloads=[download], chosen=download, description=info.get("description") or "")

    def _layer_info(self, product: Product) -> dict:
        return self._info.get(product.id) or self._json(product.id, product.id)

    def layers(self, product: Product) -> list[remote.Layer]:
        info = self._layer_info(product)
        count = self._json(f"{product.id}/query", product.id, where="1=1", returnCountOnly="true").get("count")
        geometry = info.get("geometryType")
        return [remote.Layer(
            name=table_name(info, product),
            file="",
            geometry=GEOMETRY.get(geometry, geometry) if geometry else None,
            crs=epsg((info.get("extent") or {}).get("spatialReference") or info.get("sourceSpatialReference")),
            features=count if isinstance(count, int) else None,
        )]

    def gdal_source(self, product: Product) -> tuple[str, str, str]:
        """(GDAL connection, GDAL's layer, the name `tables` knows it by) that load this layer."""
        info = self._layer_info(product)
        oid = next((f.get("name") for f in info.get("fields") or [] if f.get("type") == "esriFieldTypeOID"),
                   info.get("objectIdField"))
        params = {"where": "1=1", "outFields": "*", "returnGeometry": "true", "f": "json"}
        if oid:
            params["orderByFields"] = oid  # pages in a fixed order: none read twice, none missed
        query = urlencode(params, quote_via=quote, safe="*,")
        return f"ESRIJSON:{self.root}/{product.id}/query?{query}", "ESRIJSON", table_name(info, product)

    def download_url(self, download: Download) -> str:
        return download.url


def table_name(info: dict, product: Product) -> str:
    """How `tables` names the layer: the layer's own name, as WFS's feature types are named."""
    return info.get("name") or product.id.rsplit("/", 1)[-1]
