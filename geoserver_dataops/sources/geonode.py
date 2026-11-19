"""GeoNode: the open-source spatial data platform behind many national, humanitarian and research portals.

The Source's address is the GeoNode (https://stable.demo.geonode.org). Its published datasets are the products,
by their name in GeoNode (`geonode:rutas_espana`, as discover lists them), read through its API (/api/v2).
A dataset is loaded from the exports GeoNode lists for it, made by its own GeoServer: GeoPackage when offered,
else a zipped Shapefile, GeoJSON or GML; a raster dataset's GeoTIFF becomes a Cloud-Optimised GeoTIFF
(`raster:`). `resource` picks another export by name. The version is when the dataset was last updated.
Public datasets only: the original uploaded files need a login.

A dataset's styles are read from GeoNode's GeoServer through WMS GetStyles (its REST API needs a login), each as an
SLD of its own, the dataset's default marked so (styles.py writes them with `dataops styles`).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

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

    def styles(self, product: Product) -> list:
        """The dataset's styles, from WMS GetStyles on GeoNode's GeoServer: one Style per UserStyle."""
        from ..styles import Style

        found = [d for d in self._datasets({"filter{alternate}": product.id}, product.id).get("datasets") or [] if isinstance(d, dict)]
        wms = next((link["url"] for d in found for link in d.get("links") or []
                    if isinstance(link, dict) and link.get("link_type") == "OGC:WMS" and link.get("url")), f"{self.url}/geoserver/ows")
        try:
            r = self.session.get(wms, params={"service": "WMS", "version": "1.1.1", "request": "GetStyles", "layers": product.id},
                                 timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SourceError(f"can't read {product.id}'s styles: {e}") from None
        if r.status_code != 200:
            raise SourceError(f"{wms} answered HTTP {r.status_code} for {product.id}'s styles")
        return [Style(name=title, sld=sld, default=default, origin="GeoNode's style") for title, sld, default in split_styles(r.content)]


SLD_NS = "http://www.opengis.net/sld"
for prefix, uri in (("", SLD_NS), ("ogc", "http://www.opengis.net/ogc"), ("xlink", "http://www.w3.org/1999/xlink"),
                    ("gml", "http://www.opengis.net/gml"), ("se", "http://www.opengis.net/se")):
    ET.register_namespace(prefix, uri)


def split_styles(document: bytes) -> list[tuple[str, str, bool]]:
    """(name, SLD, default) for each UserStyle of a GetStyles answer, each made an SLD document of its own."""
    try:
        root = ET.fromstring(document)
    except ET.ParseError:
        return []
    out = []
    for layer in root.iter(f"{{{SLD_NS}}}NamedLayer"):
        name = layer.findtext(f"{{{SLD_NS}}}Name") or ""
        for user in layer.findall(f"{{{SLD_NS}}}UserStyle"):
            doc = ET.Element(f"{{{SLD_NS}}}StyledLayerDescriptor", {"version": root.get("version") or "1.0.0"})
            named = ET.SubElement(doc, f"{{{SLD_NS}}}NamedLayer")
            ET.SubElement(named, f"{{{SLD_NS}}}Name").text = name.split(":")[-1]
            named.append(user)
            ET.indent(doc, space="  ")
            sld = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(doc, encoding="unicode") + "\n"
            title = user.findtext(f"{{{SLD_NS}}}Name") or user.findtext(f"{{{SLD_NS}}}Title") or name
            default = (user.findtext(f"{{{SLD_NS}}}IsDefault") or "").strip().lower() in ("1", "true")
            out.append((title, sld, default))
    return out
