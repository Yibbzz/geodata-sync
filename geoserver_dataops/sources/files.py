"""Files in a folder or a cloud bucket: where flat files are usually dropped.

The Source's address says where:

    file:///mnt/drop/roads/                 a folder: a mounted volume, an NFS or SMB share, a laptop's disk
    s3://bucket/prefix/                     Amazon S3, or any S3-compatible store (MinIO, Cloudflare R2,
                                            Wasabi, Ceph) with `options.endpoint`
    gs://bucket/prefix/                     Google Cloud Storage
    az://account/container/prefix/          Azure Blob Storage

Every file under it that dataops can load is a product, named by its path below the address
(`2026/roads.gpkg`). A product can be a pattern (`deliveries/roads_*.zip`): the newest matching file
is loaded, which is how suppliers deliver (a new dated file each time). A file's version is when it
last changed (and its ETag or size), so a file is loaded again only once it is replaced.

Credentials, from the Source's secret: `accessKeyId` and `secretAccessKey` (S3, and Google Cloud
Storage's HMAC keys), with `sessionToken` for temporary ones; `sasToken` for Azure. Without them,
public buckets are read anonymously. Requests are signed here (AWS Signature Version 4), with no
cloud library: a download is a signed address, good for an hour, fetched like any other.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import os
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse

import requests

from . import Download, Listing, NotFound, Product, SourceError, http_session, matches
from .formats import by_extension

TIMEOUT = 60
EXPIRES = 3600  # seconds a signed address is good for
RASTERS = (".tif", ".tiff", ".asc", ".img", ".jp2", ".dem", ".bil", ".flt")
SCHEMES = ("file", "s3", "gs", "az")


@dataclass
class Entry:
    path: str  # below the Source's address
    size: int | None
    modified: str  # ISO 8601, UTC
    tag: str  # what changes when the file does: an ETag, or the size and modification time

    @property
    def version(self) -> str:
        return f"{self.modified} {self.tag[:16]}"


def iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def label(path: str) -> str | None:
    """The format a file loads as, from its name, or None if dataops can't load it."""
    return by_extension(path) or ("GeoTIFF" if path.lower().endswith(RASTERS) else None)


# --- AWS Signature Version 4, as a signed address (query authentication)

def presign(method: str, url: str, params: dict, region: str, key: str, secret: str, token: str | None,
            now: datetime | None = None, expires: int = EXPIRES) -> str:
    """`url` with `params` and a SigV4 signature in its query: S3, Google Cloud Storage (HMAC keys), MinIO."""
    now = now or datetime.now(timezone.utc)
    stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    scope = f"{day}/{region}/s3/aws4_request"
    parsed = urlparse(url)
    query = {**params, "X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": f"{key}/{scope}",
             "X-Amz-Date": stamp, "X-Amz-Expires": str(expires), "X-Amz-SignedHeaders": "host"}
    if token:
        query["X-Amz-Security-Token"] = token
    canonical_query = "&".join(f"{quote(k, safe='-_.~')}={quote(str(v), safe='-_.~')}" for k, v in sorted(query.items()))
    request = "\n".join([method, quote(parsed.path or "/", safe="/-_.~"), canonical_query,
                         f"host:{parsed.netloc}\n", "host", "UNSIGNED-PAYLOAD"])
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(request.encode()).hexdigest()])
    signing_key = f"AWS4{secret}".encode()
    for part in (day, region, "s3", "aws4_request"):
        signing_key = hmac.new(signing_key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{parsed.scheme}://{parsed.netloc}{quote(parsed.path or '/', safe='/-_.~')}?{canonical_query}&X-Amz-Signature={signature}"


# --- where the files are

class Folder:
    def __init__(self, root: str):
        self.root = Path(root)
        if not self.root.is_dir():
            raise SourceError(f"{self.root} isn't a folder here: is the volume or share mounted?")

    def entries(self) -> list[Entry]:
        found = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=True):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith("."):
                    continue
                path = Path(dirpath) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                found.append(Entry(path.relative_to(self.root).as_posix(), st.st_size,
                                   iso(datetime.fromtimestamp(st.st_mtime, timezone.utc)), f"{st.st_size}-{st.st_mtime_ns}"))
        return found

    def address(self, path: str) -> str:
        return (self.root / path).as_uri()

    def fetch(self, path: str, dest: Path) -> str:
        sha = hashlib.sha256()
        with open(self.root / path, "rb") as src, open(dest, "wb") as out:
            while chunk := src.read(1 << 20):
                out.write(chunk)
                sha.update(chunk)
        return sha.hexdigest()


class Bucket:
    """S3 and what speaks its API: listed with ListObjects, read through signed addresses."""

    def __init__(self, bucket: str, prefix: str, endpoint: str | None, region: str | None, path_style: bool,
                 credentials: Callable[[str], str | None], session: requests.Session, google: bool = False):
        self.bucket, self.prefix, self.session = bucket, prefix, session
        self.key, self.secret = credentials("accessKeyId"), credentials("secretAccessKey")
        self.token = credentials("sessionToken")
        self.google = google
        if google:
            self.base, self.region = f"https://storage.googleapis.com/{bucket}", region or "auto"
        elif endpoint:
            endpoint = endpoint.rstrip("/")
            self.base = f"{endpoint}/{bucket}" if path_style else endpoint.replace("://", f"://{bucket}.", 1)
            self.region = region or "us-east-1"
        else:
            self.region = region or "us-east-1"
            self.base = f"https://{bucket}.s3.{self.region}.amazonaws.com"
        self.region_known = bool(region) or google or bool(endpoint)

    def url(self, key: str = "", params: dict | None = None, method: str = "GET") -> str:
        address = f"{self.base}/{quote(key, safe='/-_.~')}" if key else f"{self.base}/"
        if self.key and self.secret:
            return presign(method, address, params or {}, self.region, self.key, self.secret, self.token)
        return f"{address}?{urlencode(params)}" if params else address

    def _get(self, key: str = "", params: dict | None = None) -> requests.Response:
        try:
            r = self.session.get(self.url(key, params), timeout=TIMEOUT)
        except requests.RequestException as e:
            raise SourceError(f"can't reach {self.base}: {e}") from None
        moved = r.headers.get("x-amz-bucket-region")
        if r.status_code in (301, 400) and moved and moved != self.region and not self.region_known:
            # Another region than guessed: AWS says which
            self.region, self.region_known = moved, True
            self.base = f"https://{self.bucket}.s3.{moved}.amazonaws.com"
            return self._get(key, params)
        if r.status_code in (401, 403):
            hint = "" if self.key else " (no credentials: set accessKeyId and secretAccessKey in the Source's secret)"
            raise SourceError(f"{self.base} refused access (HTTP {r.status_code}){hint}")
        if r.status_code == 404:
            raise SourceError(f"{self.base}: no such bucket, or no access to it")
        if r.status_code != 200:
            raise SourceError(f"{self.base} answered HTTP {r.status_code}: {' '.join(r.text.split())[:200]}")
        return r

    def entries(self) -> list[Entry]:
        found, marker = [], ""
        while True:
            params = {"prefix": self.prefix, **({"marker": marker} if marker else {})}
            try:
                root = ET.fromstring(self._get("", params).content)
            except ET.ParseError:
                raise SourceError(f"{self.base} didn't answer with a bucket listing") from None
            ns = root.tag[: root.tag.index("}") + 1] if root.tag.startswith("{") else ""
            contents = root.findall(f"{ns}Contents")
            for c in contents:
                key = c.findtext(f"{ns}Key") or ""
                if not key or key.endswith("/"):
                    continue
                modified = c.findtext(f"{ns}LastModified") or ""
                found.append(Entry(key[len(self.prefix):], int(c.findtext(f"{ns}Size") or 0) or None,
                                   modified[:19] + "Z" if modified else "", (c.findtext(f"{ns}ETag") or "").strip('"')))
            if root.findtext(f"{ns}IsTruncated") != "true" or not contents:
                return found
            marker = root.findtext(f"{ns}NextMarker") or contents[-1].findtext(f"{ns}Key")

    def address(self, path: str) -> str:
        return self.url(self.prefix + path)


class Container:
    """Azure Blob Storage: listed with List Blobs, read with the SAS token, if any, in the address."""

    def __init__(self, account: str, container: str, prefix: str, endpoint: str | None,
                 credentials: Callable[[str], str | None], session: requests.Session):
        self.base = f"{(endpoint or f'https://{account}.blob.core.windows.net').rstrip('/')}/{container}"
        self.prefix, self.session = prefix, session
        self.sas = (credentials("sasToken") or "").lstrip("?")

    def _with_sas(self, url: str) -> str:
        return f"{url}{'&' if '?' in url else '?'}{self.sas}" if self.sas else url

    def entries(self) -> list[Entry]:
        found, marker = [], ""
        while True:
            params = {"restype": "container", "comp": "list", "prefix": self.prefix, **({"marker": marker} if marker else {})}
            try:
                r = self.session.get(self._with_sas(f"{self.base}?{urlencode(params)}"), timeout=TIMEOUT)
            except requests.RequestException as e:
                raise SourceError(f"can't reach {self.base}: {e}") from None
            if r.status_code in (401, 403, 404, 409):  # 409: the container doesn't allow public access
                hint = "" if self.sas else " (no sasToken in the Source's secret: only public containers can be listed)"
                raise SourceError(f"{self.base} refused the listing (HTTP {r.status_code}){hint}")
            if r.status_code != 200:
                raise SourceError(f"{self.base} answered HTTP {r.status_code}")
            try:
                root = ET.fromstring(r.content)
            except ET.ParseError:
                raise SourceError(f"{self.base} didn't answer with a blob listing") from None
            for blob in root.iter("Blob"):
                name, props = blob.findtext("Name") or "", blob.find("Properties")
                if props is None or not name:
                    continue
                modified = props.findtext("Last-Modified")
                found.append(Entry(name[len(self.prefix):], int(props.findtext("Content-Length") or 0) or None,
                                   iso(parsedate_to_datetime(modified)) if modified else "", (props.findtext("Etag") or "").strip('"')))
            marker = root.findtext("NextMarker") or ""
            if not marker:
                return found

    def address(self, path: str) -> str:
        return self._with_sas(f"{self.base}/{quote(self.prefix + path, safe='/-_.~')}")


# --- the source type

class Files:
    name = "files"
    formats = ["GeoPackage", "Shapefile", "GeoJSON", "FlatGeobuf", "GeoTIFF", "ZIP"]

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None,
                 credentials: Callable[[str], str | None] | None = None, options: dict | None = None):
        if not url:
            raise SourceError("a files source needs where the files are: spec.url in the Source, such as file:///mnt/drop/, "
                              "s3://bucket/prefix/, gs://bucket/prefix/ or az://account/container/prefix/")
        self.url, options = url, options or {}
        credentials = credentials or (lambda key: None)
        session = session or http_session()
        parsed = urlparse(url)
        prefix = parsed.path.lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        if parsed.scheme == "file":
            self.where = Folder(parsed.path)
        elif parsed.scheme in ("s3", "gs"):
            self.where = Bucket(parsed.netloc, prefix, options.get("endpoint"), options.get("region"),
                                bool(options.get("pathStyle", bool(options.get("endpoint")))), credentials, session,
                                google=parsed.scheme == "gs")
        elif parsed.scheme == "az":
            container, _, rest = prefix.partition("/")
            if not container:
                raise SourceError(f"{url}: give az://<account>/<container>/<prefix>")
            self.where = Container(parsed.netloc, container, rest, options.get("endpoint"), credentials, session)
        else:
            raise SourceError(f"{url}: a files source reads {', '.join(s + '://' for s in SCHEMES)} addresses")

    def _product(self, entry: Entry, pattern: str | None = None) -> Product:
        fmt = label(entry.path)
        download = Download(format=fmt or "unknown", url=self.where.address(entry.path),
                            file_name=entry.path.rsplit("/", 1)[-1], size=entry.size, id=entry.path, name=entry.path,
                            loadable=fmt is not None)
        # A pattern's version leads with the file it picked: which delivery was loaded shows in sync's output
        version = f"{entry.path.rsplit('/', 1)[-1]} {entry.version}" if pattern else entry.version
        return Product(id=pattern or entry.path, name=entry.path, version=version, formats=[download.format],
                       downloads=[download], loads_as=fmt, chosen=download if fmt else None,
                       description=f"{self.url}{entry.path}")

    def products(self, search: str | None = None, limit: int | None = None) -> Listing:
        found = [self._product(e) for e in self.where.entries() if label(e.path)]
        found = [p for p in found if matches(p, search)]
        return Listing(found[:limit] if limit else found, len(found))

    def product(self, path: str, resource: str | None = None) -> Product:
        """A file by its path below the address, or the newest file matching a pattern (`roads_*.zip`)."""
        if resource:
            raise SourceError("a file has no resources to pick; a pattern in the product picks the newest of several files")
        entries = self.where.entries()
        if any(c in path for c in "*?["):
            hits = [e for e in entries if fnmatch.fnmatchcase(e.path, path) and label(e.path)]
            if not hits:
                raise NotFound(path)
            newest = max(hits, key=lambda e: (e.modified, e.path))
            return self._product(newest, pattern=path)
        entry = next((e for e in entries if e.path == path.lstrip("/")), None)
        if entry is None:
            raise NotFound(path)
        return self._product(entry)

    def download_url(self, download: Download) -> str:
        """A signed address for a bucket's file (fresh: good for an hour); a folder's file:// address."""
        return self.where.address(download.id)

    def fetch(self, download: Download, dest: Path) -> str:
        """Copy the file to `dest`; returns its SHA-256."""
        if isinstance(self.where, Folder):
            try:
                return self.where.fetch(download.id, dest)
            except OSError as e:
                raise SourceError(f"can't read {download.id} in {self.url}: {e.strerror or e}") from None
        from ..ingest import download as http_download
        return http_download(self.download_url(download), dest)

