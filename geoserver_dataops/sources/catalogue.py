"""Metadata catalogues: OGC API Records, and CSW (Catalogue Service for the Web, 2.0.2).

Catalogues are how much of Europe (every INSPIRE geoportal), and GeoNetwork sites the world over, list
their data: each record describes a dataset and links to its files and services. A record is a product,
its links are its downloads, and sync loads the best file among them, as for a CKAN package; a link to a
WFS, OGC API Features or ArcGIS service says how to discover that instead. The version is when the
record last changed.

ArcGIS Hub's search (…/api/search/v1) is an OGC API Records catalogue too.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

import requests

from . import Download, Listing, NotFound, Product, SourceError, http_session
from .formats import FILE_LABELS, classify, rank, service
from .url import resolve

TIMEOUT = 60
DEFAULT_LIMIT = 20
GEOJSON = "application/geo+json, application/json;q=0.9"
# Link relations that point at the data rather than at web pages about it
DATA_RELS = {"enclosure", "download", "data", "item", "alternate", "service", "related", ""}


def when(value) -> str | None:
    """A record's time as text. ArcGIS Hub gives milliseconds since 1970 (1794926649000), not a date."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if abs(value) > 1e11 else value  # milliseconds, else seconds
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        except (OverflowError, OSError, ValueError):
            return str(value)
    return str(value)


ARCGIS_ITEM_DATA = "https://www.arcgis.com/sharing/rest/content/items/{id}/data"
SUFFIXES = {"GeoPackage": ".gpkg", "GeoJSON": ".geojson", "KML": ".kml", "FlatGeobuf": ".fgb"}  # else a zip


def hub_downloads(record: dict, props: dict) -> list[Download]:
    """ArcGIS Hub's records link to nothing: the data is an ArcGIS Online item. Its `url` is the service it
    publishes (a FeatureServer: the arcgis source type reads it); an item that is a file (`type`: GeoJson,
    Shapefile, File Geodatabase) is downloaded from ArcGIS Online by its ID."""
    found = []
    if str(props.get("url") or "").startswith(("http://", "https://")):
        found.append(link_download(props["url"], props.get("type"), props.get("title") or "", props["url"]))
    ident = str(record.get("id") or "")
    label = classify(props.get("type"))
    if label and re.fullmatch(r"[0-9a-f]{32}", ident):
        d = link_download(ARCGIS_ITEM_DATA.format(id=ident), props.get("type"), props.get("title") or "", ident)
        d.file_name = f"{ident}{SUFFIXES.get(label, '.zip')}"
        found.append(d)
    return found


def link_download(href: str, fmt: str | None = None, title: str = "", ident: str = "") -> Download:
    """A record's link as a download: a file dataops loads, a service, or neither."""
    protocol = (fmt or "").replace(":", " ").replace("-", " ")  # CSW's protocols: OGC:WFS, ESRI:REST
    label = classify(fmt, href, fmt, title)
    kind = service(protocol, href, title) or service(fmt, href, title)
    return Download(
        format=label or kind or (fmt or "").removeprefix("https://www.iana.org/assignments/media-types/") or "unknown",
        url=href,
        file_name=unquote(urlparse(href).path.rsplit("/", 1)[-1]),
        id=ident,
        name=title.strip(),
        loadable=label is not None and href.startswith(("http://", "https://")),
    )


def choose(downloads: list[Download], resource: str | None, product: str) -> Download | None:
    """The download sync uses: the one `resource` names (an ID, a name, or the link itself), else the best file."""
    if not resource:
        return min((d for d in downloads if d.loadable), key=lambda d: rank(d.format), default=None)
    wanted = resource.lower()
    picked = [d for d in downloads if wanted in (d.id.lower(), d.name.lower(), d.url.lower())]
    if not picked:
        names = ", ".join(d.name or d.id or d.url for d in downloads)
        raise SourceError(f"{product} has no link '{resource}' (its links: {names})")
    if not picked[0].loadable:
        raise SourceError(f"link '{resource}' is {picked[0].format}, not a file dataops can load")
    return picked[0]


def finish(product: Product, resource: str | None) -> Product:
    product.chosen = choose(product.downloads, resource, product.id)
    product.loads_as = product.chosen.format if product.chosen else None
    product.formats = list(dict.fromkeys(d.format for d in product.downloads))
    return product


class _Catalogue:
    formats = FILE_LABELS

    def __init__(self, session: requests.Session | None):
        self.session = session or http_session()

    def _get(self, url: str, what: str, accept: str, params: dict | None = None) -> requests.Response:
        try:
            r = self.session.get(url, params=params, headers={"Accept": accept}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SourceError(f"can't reach {url}: {e}") from None
        if r.status_code == 404:
            raise NotFound(what)
        if r.status_code in (401, 403):
            raise SourceError(f"{url} refused the request (HTTP {r.status_code})")
        if r.status_code != 200:
            raise SourceError(f"{url} answered HTTP {r.status_code}")
        return r

    def download_url(self, download: Download) -> str:
        return resolve(self.session, download.url)


# --- OGC API Records


class OGCAPIRecords(_Catalogue):
    name = "OGC API Records catalogue"

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        if not url:
            raise SourceError("an ogc-api-records source needs the catalogue's address: spec.url in the Source, or --url "
                              "(an OGC API landing page, or one catalogue: …/collections/<catalogue>)")
        super().__init__(session)
        base = url.split("?", 1)[0].rstrip("/")
        self.url, _, rest = base.partition("/collections")
        self._catalogue = rest.strip("/").split("/")[0] or None

    def _json(self, url: str, what: str, params: dict | None = None) -> dict:
        r = self._get(url, what, GEOJSON, params)
        try:
            data = r.json()
        except ValueError:
            raise SourceError(f"{url} didn't answer with JSON; is it an OGC API Records address?") from None
        if not isinstance(data, dict):
            raise SourceError(f"{url} didn't answer with a JSON object")
        return data

    def catalogue(self) -> str:
        """The catalogue (a collection of records) the address names, or the API's first."""
        if self._catalogue is None:
            data = self._json(f"{self.url}/collections", "collections")
            records = [c["id"] for c in data.get("collections") or []
                       if isinstance(c, dict) and c.get("id") and c.get("itemType") == "record"]
            if not records:
                raise SourceError(f"{self.url} has no catalogue of records (no collection with itemType record)")
            self._catalogue = records[0]
        return self._catalogue

    def _product(self, record: dict) -> Product:
        props = record.get("properties") or {}
        links = (record.get("links") or []) + (props.get("associations") or []) + (record.get("associations") or [])
        downloads = [link_download(link["href"], link.get("type"), link.get("title") or link.get("name") or "",
                                   link.get("href", ""))
                     for link in links if isinstance(link, dict) and link.get("href")
                     and (link.get("rel") or "") in DATA_RELS]
        if not any(d.loadable for d in downloads):
            downloads += hub_downloads(record, props)
        return Product(
            id=str(record.get("id")), name=props.get("title") or str(record.get("id")),
            version=when(props.get("updated") or props.get("modified") or props.get("created")),
            downloads=downloads, description=props.get("description") or "")

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        params = {"limit": limit or DEFAULT_LIMIT}
        if search:
            params["q"] = ",".join(search.split())
        data = self._json(f"{self.url}/collections/{self.catalogue()}/items", "records", params)
        found = [finish(self._product(r), None) for r in data.get("features") or [] if isinstance(r, dict) and r.get("id")]
        matched = data.get("numberMatched")
        return Listing(found, matched if isinstance(matched, int) else None)

    def product(self, record_id: str, resource: str | None = None) -> Product:
        record = self._json(f"{self.url}/collections/{self.catalogue()}/items/{record_id}", record_id)
        return finish(self._product(record), resource)


# --- CSW 2.0.2

NS = {"csw": "http://www.opengis.net/cat/csw/2.0.2", "dc": "http://purl.org/dc/elements/1.1/",
      "dct": "http://purl.org/dc/terms/", "ows": "http://www.opengis.net/ows"}
CSW_PARAMS = {"service": "CSW", "version": "2.0.2", "outputSchema": NS["csw"], "elementSetName": "full"}


def parse(xml: bytes, url: str) -> ET.Element:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise SourceError(f"{url} didn't answer with XML; is it a CSW address?") from None
    if root.tag.endswith("ExceptionReport"):
        text = " ".join(t.strip() for t in root.itertext() if t.strip())
        raise SourceError(f"the catalogue answered with an error: {text or 'no details'}")
    return root


def csw_product(record: ET.Element) -> Product:
    def text(path: str) -> str:
        """An element's text on one line. GeoNetwork writes each of a record's languages on its own line of
        the one element, often the same words again: each different line once, joined by " / "."""
        found = record.find(path, NS)
        lines = [" ".join(line.split()) for line in (found.text or "").splitlines()] if found is not None else []
        return " / ".join(dict.fromkeys(line for line in lines if line))

    ident = text("dc:identifier")
    downloads = []
    for uri in record.findall("dc:URI", NS) + record.findall("dct:references", NS):
        href = (uri.text or "").strip()
        if href:
            downloads.append(link_download(href, uri.get("protocol") or uri.get("scheme"),
                                           uri.get("name") or uri.get("description") or "", href))
    return Product(id=ident, name=text("dc:title") or ident, version=text("dct:modified") or text("dc:date") or None,
                   downloads=downloads, description=text("dct:abstract") or text("dc:description"))


class CSW(_Catalogue):
    name = "CSW catalogue"

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        if not url:
            raise SourceError("a csw source needs the catalogue's address: spec.url in the Source, or --url "
                              "(e.g. https://<geonetwork>/geonetwork/srv/eng/csw)")
        super().__init__(session)
        self.url = url.split("?", 1)[0].rstrip("/")

    def _xml(self, params: dict, what: str) -> ET.Element:
        r = self._get(self.url, what, "application/xml", {**CSW_PARAMS, **params})
        return parse(r.content, self.url)

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        params = {"request": "GetRecords", "typeNames": "csw:Record", "resultType": "results",
                  "maxRecords": limit or DEFAULT_LIMIT}
        if search:
            words = [w.replace("'", "''") for w in search.split()]
            params.update(constraintLanguage="CQL_TEXT", constraint_language_version="1.1.0",
                          constraint=" AND ".join(f"AnyText LIKE '%{w}%'" for w in words))
        root = self._xml(params, "records")
        results = root.find("csw:SearchResults", NS)
        if results is None:
            raise SourceError(f"{self.url} didn't answer GetRecords with search results; is it a CSW address?")
        found = [finish(csw_product(r), None) for r in results.findall("csw:Record", NS)]
        matched = results.get("numberOfRecordsMatched")
        return Listing(found, int(matched) if matched and matched.isdigit() else None)

    def product(self, record_id: str, resource: str | None = None) -> Product:
        root = self._xml({"request": "GetRecordById", "id": record_id}, record_id)
        record = root.find("csw:Record", NS)
        if record is None:
            raise NotFound(record_id)
        return finish(csw_product(record), resource)
