"""A dataops source type for the USGS earthquake catalogue (earthquake.usgs.gov): an example plugin.

Install it beside dataops (`pip install ./docs/plugin-example`), and a Source can use it:

    kind: Source
    spec: { type: usgs-earthquakes }
    ---
    kind: Dataset
    spec:
      source: quakes
      product: significant            # or 4.5, 2.5, 1.0, all: the smallest magnitude
      options: { days: 30 }           # how far back

docs/plugins.md explains each part.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from geoserver_dataops.sources import Download, Listing, NotFound, Product, SourceError, http_session, matches

API = "https://earthquake.usgs.gov/fdsnws/event/1/query"
MAGNITUDES = {"significant": None, "4.5": 4.5, "2.5": 2.5, "1.0": 1.0, "all": None}


class Earthquakes:
    name = "USGS earthquakes"
    formats = ["GeoJSON"]

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None,
                 credentials=None, options: dict | None = None):
        # url: the Source's spec.url (another catalogue speaking the same API); credentials: a function giving a key
        # of the Source's secret (this API needs none); options: the Source's spec.options
        self.url = url or API
        self.session = session or http_session()

    def _product(self, name: str, days: int = 30) -> Product:
        query = {"format": "geojson", "orderby": "time-asc",
                 "starttime": (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")}
        if name == "significant":
            query["minsig"] = 600
        elif MAGNITUDES[name] is not None:
            query["minmagnitude"] = MAGNITUDES[name]
        download = Download(format="GeoJSON", url=self.url, file_name="earthquakes.geojson", id=name, name=name,
                            area=json.dumps(query))
        return Product(id=name, name=f"Earthquakes, {name}", version=None, formats=["GeoJSON"], downloads=[download],
                       loads_as="GeoJSON", chosen=download, description=f"USGS earthquakes: {name}")

    # --- what `dataops discover` shows

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        found = [p for p in (self._product(name) for name in MAGNITUDES) if matches(p, search)]
        return Listing(found[:limit] if limit else found, len(found))

    def product(self, name: str, resource: str | None = None) -> Product:
        if name not in MAGNITUDES:
            raise NotFound(name)
        return self._product(name)

    # --- what `dataops sync` uses

    def selection(self, name: str, options: dict) -> Product:
        """The product with the Dataset's `options` (here, how many days back)."""
        if name not in MAGNITUDES:
            raise NotFound(name)
        return self._product(name, int(options.get("days", 30)))

    def download_url(self, download: Download) -> str:
        """Where `discover` reads the layers from."""
        return requests.Request("GET", download.url, params=json.loads(download.area)).prepare().url

    def fetch(self, download: Download, dest: Path) -> str:
        """Write the file sync loads, and return its SHA-256. The API stamps each answer with when it made it
        (metadata.generated): left out, the same earthquakes give the same digest, so they aren't loaded again."""
        try:
            r = self.session.get(download.url, params=json.loads(download.area), timeout=120)
        except requests.RequestException as e:
            raise SourceError(f"can't reach {self.url}: {e}") from None
        if r.status_code != 200:
            raise SourceError(f"{self.url} answered HTTP {r.status_code}")
        data = r.json()
        data.get("metadata", {}).pop("generated", None)
        body = json.dumps(data, sort_keys=True).encode()
        dest.write_bytes(body)
        return hashlib.sha256(body).hexdigest()
