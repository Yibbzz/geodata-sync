"""INSPIRE Atom download services: how most European agencies publish their datasets' files
(PDOK in the Netherlands, the German states' geoportals, Ireland, the Nordic countries).

The Source's address is the service feed, which lists datasets; each dataset has a feed of its own
listing its files (a GeoPackage, a zipped Shapefile, the same in several CRSs). A dataset is a product,
named by its feed's file name (`wijk_en_buurtkaart_2023` for …/atom/wijk_en_buurtkaart_2023.xml),
and its version is when its feed last changed. Files are chosen as a catalogue record's are: the best
format, or the one `resource` names. A dataset split into many files (one per map sheet) loads one of them.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

from . import Download, Listing, NotFound, Product, SourceError, http_session, matches
from .catalogue import finish, link_download
from .formats import FILE_LABELS

TIMEOUT = 60
A = "{http://www.w3.org/2005/Atom}"
DLS = "{http://inspire.ec.europa.eu/schemas/inspire_dls/1.0}"
ATOM_TYPE = "application/atom+xml"


def short_id(url: str) -> str:
    """A dataset's ID: its feed's file name without .xml."""
    return re.sub(r"\.xml$", "", urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]) or url


def text(element: ET.Element, tag: str) -> str:
    return " ".join((element.findtext(f"{A}{tag}") or "").split())


def links(element: ET.Element) -> list[dict]:
    return [dict(link.attrib) for link in element.findall(f"{A}link") if link.get("href")]


class Atom:
    name = "INSPIRE Atom download service"
    formats = FILE_LABELS

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None):
        if not url:
            raise SourceError("an atom source needs the download service's feed: spec.url in the Source, or --url "
                              "(e.g. https://service.pdok.nl/cbs/wijkenbuurten/2023/atom/index.xml)")
        self.url = url
        self.session = session or http_session()

    def _feed(self, url: str, what: str) -> ET.Element:
        try:
            r = self.session.get(url, headers={"Accept": f"{ATOM_TYPE}, application/xml;q=0.9"}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SourceError(f"can't reach {url}: {e}") from None
        if r.status_code == 404:
            raise NotFound(what)
        if r.status_code != 200:
            raise SourceError(f"{url} answered HTTP {r.status_code}")
        try:
            root = ET.fromstring(r.content)
        except ET.ParseError:
            raise SourceError(f"{url} didn't answer with XML; is it an Atom feed?") from None
        if root.tag != f"{A}feed":
            raise SourceError(f"{url} isn't an Atom feed")
        return root

    def _datasets(self) -> list[tuple[ET.Element, str]]:
        """(entry, its dataset feed's address) for each dataset of the service feed."""
        found = []
        for entry in self._feed(self.url, "the service feed").findall(f"{A}entry"):
            feed = next((link["href"] for link in links(entry) if ATOM_TYPE in (link.get("type") or "")
                         and link.get("rel", "alternate") == "alternate"), None)
            ident = text(entry, "id")
            found.append((entry, feed or (ident if ident.startswith(("http://", "https://")) else "")))
        return found

    @staticmethod
    def _product(entry: ET.Element, feed: str) -> Product:
        return Product(id=short_id(feed or text(entry, "id")), name=text(entry, "title") or short_id(feed),
                       version=text(entry, "updated") or None, description=text(entry, "summary"))

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        """The datasets, each with the format sync would load: that takes its own feed, read for the ones listed."""
        datasets = [(e, f) for e, f in self._datasets() if matches(self._product(e, f), search)]
        shown = datasets[:limit] if limit else datasets

        def described(pair):
            entry, feed = pair
            try:
                return self._described(entry, feed, None)
            except SourceError:  # a dataset whose feed is missing or broken is still listed
                return self._product(entry, feed)

        with ThreadPoolExecutor(max_workers=8) as pool:
            found = list(pool.map(described, shown))
        return Listing(found, len(datasets))

    def product(self, dataset: str, resource: str | None = None) -> Product:
        for entry, feed in self._datasets():
            if dataset in (short_id(feed or text(entry, "id")), feed, text(entry, "id")):
                break
        else:
            raise NotFound(dataset)
        return self._described(entry, feed, resource)

    def _described(self, entry: ET.Element, feed: str, resource: str | None) -> Product:
        """A dataset with its files, from its own feed."""
        p = self._product(entry, feed)
        root = self._feed(feed, p.id) if feed else None
        if root is not None:
            p.version = text(root, "updated") or p.version
        # Each file entry's links: a file, or (with a dataset feed of its own nowhere) the service entry's own
        entries = root.findall(f"{A}entry") if root is not None else [entry]
        p.downloads = [download(e, link) for e in entries for link in links(e)
                       if link.get("rel", "alternate") in ("alternate", "section", "enclosure")
                       and ATOM_TYPE not in (link.get("type") or "")]
        return finish(p, resource)

    def download_url(self, download: Download) -> str:
        return download.url


def download(entry: ET.Element, link: dict) -> Download:
    crs = next((c.get("label") or c.get("term") for c in entry.findall(f"{A}category")), "")
    d = link_download(link["href"], link.get("type"), link.get("title") or text(entry, "title"), link["href"])
    d.subformat = crs or ""
    try:
        d.size = int(link.get("length") or 0) or None
    except ValueError:
        pass
    return d
