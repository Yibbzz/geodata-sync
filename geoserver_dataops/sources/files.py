"""Files in a folder or a cloud bucket: where flat files are usually dropped.

The Source's address says where:

    /mnt/drop/roads/                        a folder: a mounted volume, an NFS or SMB share, a laptop's disk
                                            (also file:///mnt/drop/roads/, D:\\drop\\)
    sftp://user@host/path/                  an SFTP server (the path is absolute); its key pinned
    ftp://host/path/, ftps://host/path/     an FTP server, or FTP over TLS (the path is relative to the login's)
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

import base64
import fnmatch
import hashlib
import hmac
import os
import re
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
SCHEMES = ("file", "s3", "gs", "az", "sftp", "ftp", "ftps")
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")


def is_local_path(url: str) -> bool:
    """A plain path rather than an address: /srv/drop/, D:\\drop\\."""
    return url.startswith("/") or bool(WINDOWS_DRIVE.match(url))


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


def walk_dirs(root: str, list_dir: Callable[[str], list[tuple[str, bool, int | None, float | None]]]) -> list[Entry]:
    """Every file below `root` on a server listed one folder at a time: list_dir(folder) gives
    (name, is a folder, size, modified as a timestamp)."""
    found, folders = [], [""]
    while folders:
        rel = folders.pop()
        for name, is_dir, size, mtime in sorted(list_dir(f"{root}{rel}")):
            if name.startswith(".") or name in ("", "."):
                continue
            if is_dir:
                folders.append(f"{rel}{name}/")
            else:
                found.append(Entry(f"{rel}{name}", size, iso(datetime.fromtimestamp(mtime or 0, timezone.utc)),
                                   f"{size}-{int(mtime or 0)}"))
    return sorted(found, key=lambda e: e.path)


class Ftp:
    """An FTP server (ftp://), or FTP over TLS (ftps://): a supplier's drop box. Login from the Source's
    secret (username, password), else anonymous."""

    def __init__(self, host: str, port: int | None, root: str, tls: bool, credentials: Callable[[str], str | None]):
        # ftp://host/path/ is relative to the folder the login lands in (RFC 1738), as curl reads it
        self.host, self.port, self.root, self.tls = host, port or 21, root, tls
        self.user, self.password = credentials("username") or "anonymous", credentials("password") or ""

    def _connect(self):
        import ftplib

        ftp = (ftplib.FTP_TLS if self.tls else ftplib.FTP)(timeout=TIMEOUT)
        try:
            ftp.connect(self.host, self.port)
            ftp.login(self.user, self.password)
            if self.tls:
                ftp.prot_p()  # the files too, not only the login
        except (ftplib.Error, OSError) as e:
            raise SourceError(f"can't log in to ftp{'s' if self.tls else ''}://{self.host}:{self.port} as {self.user}: {e}") from None
        return ftp

    def _list(self, ftp, folder: str):
        import ftplib

        try:
            return [(name, facts.get("type") == "dir", int(facts["size"]) if "size" in facts else None,
                     datetime.strptime(facts["modify"][:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
                     if "modify" in facts else None)
                    for name, facts in ftp.mlsd(folder, facts=["type", "size", "modify"]) if facts.get("type") in ("dir", "file")]
        except ftplib.error_perm:  # an old server without MLSD: names, then each file's size and time
            out = []
            for path in ftp.nlst(folder):
                name = path.rstrip("/").rsplit("/", 1)[-1]
                try:
                    size = ftp.size(f"{folder}{name}")
                    stamp = ftp.voidcmd(f"MDTM {folder}{name}").split()[-1]
                    out.append((name, False, size, datetime.strptime(stamp[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()))
                except ftplib.error_perm:  # not a file: a folder
                    out.append((name, True, None, None))
            return out

    def entries(self) -> list[Entry]:
        import ftplib

        ftp = self._connect()
        try:
            return walk_dirs(self.root, lambda folder: self._list(ftp, folder))
        except (ftplib.Error, OSError) as e:
            raise SourceError(f"can't list ftp://{self.host}/{self.root}: {e}") from None
        finally:
            ftp.close()

    def address(self, path: str) -> str:
        return f"ftp{'s' if self.tls else ''}://{self.host}/{self.root}{path}"

    def fetch(self, path: str, dest: Path) -> str:
        import ftplib

        sha = hashlib.sha256()
        ftp = self._connect()
        try:
            with open(dest, "wb") as out:
                def write(chunk: bytes):
                    out.write(chunk)
                    sha.update(chunk)
                ftp.retrbinary(f"RETR {self.root}{path}", write, blocksize=1 << 20)
        except (ftplib.Error, OSError) as e:
            raise SourceError(f"can't download {path} from ftp://{self.host}: {e}") from None
        finally:
            ftp.close()
        return sha.hexdigest()


class Sftp:
    """An SFTP server (sftp://user@host/path/). Login from the Source's secret: privateKey (with passphrase if it
    has one) or password. The server's key must be pinned, options.hostKey (SHA256:..., as ssh-keygen -lf shows it)
    or knownHosts in the secret (lines as in ~/.ssh/known_hosts): otherwise anyone between could stand in for it."""

    def __init__(self, host: str, port: int | None, user: str | None, root: str,
                 credentials: Callable[[str], str | None], options: dict):
        self.host, self.port, self.root = host, port or 22, "/" + root
        self.user = user or credentials("username")
        self.credentials, self.host_key, self.known = credentials, options.get("hostKey"), credentials("knownHosts")
        if not self.user:
            raise SourceError(f"sftp://{host}: give the user, sftp://<user>@{host}/..., or username in the Source's secret")
        if not self.host_key and not self.known:
            raise SourceError(f"sftp://{host}: pin the server's key, options.hostKey: SHA256:... (ssh-keyscan -p {self.port} "
                              f"{host} | ssh-keygen -lf -), or knownHosts in the Source's secret")

    def _trusted(self, key) -> bool:
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")
        if self.host_key:
            return hmac.compare_digest(self.host_key.strip(), fingerprint)
        wanted = (key.get_name(), key.get_base64())
        return any(tuple(line.split()[1:3]) == wanted for line in self.known.splitlines() if len(line.split()) >= 3)

    def _connect(self):
        try:
            import paramiko
        except ImportError:
            raise SourceError("reading SFTP needs paramiko: pip install 'geoserver-dataops[sftp]' (the image has it)") from None
        try:
            transport = paramiko.Transport((self.host, self.port))
            transport.start_client(timeout=TIMEOUT)
            if not self._trusted(transport.get_remote_server_key()):
                transport.close()
                raise SourceError(f"sftp://{self.host}: the server's key isn't the pinned one; if it really changed, pin the new one")
            private = self.credentials("privateKey")
            if private:
                transport.auth_publickey(self.user, private_key(private, self.credentials("passphrase")))
            else:
                transport.auth_password(self.user, self.credentials("password") or "")
            return paramiko.SFTPClient.from_transport(transport)
        except (paramiko.SSHException, OSError) as e:
            raise SourceError(f"can't log in to sftp://{self.user}@{self.host}:{self.port}: {e}") from None

    def entries(self) -> list[Entry]:
        import stat

        sftp = self._connect()
        try:
            return walk_dirs(self.root, lambda folder: [(a.filename, stat.S_ISDIR(a.st_mode or 0), a.st_size, a.st_mtime)
                                                        for a in sftp.listdir_attr(folder)])
        except OSError as e:
            raise SourceError(f"can't list sftp://{self.host}{self.root}: {e}") from None
        finally:
            sftp.get_channel().get_transport().close()

    def address(self, path: str) -> str:
        return f"sftp://{self.user}@{self.host}{self.root}{path}"

    def fetch(self, path: str, dest: Path) -> str:
        sha = hashlib.sha256()
        sftp = self._connect()
        try:
            with sftp.open(f"{self.root}{path}", "rb") as src, open(dest, "wb") as out:
                src.prefetch()
                while chunk := src.read(1 << 20):
                    out.write(chunk)
                    sha.update(chunk)
        except OSError as e:
            raise SourceError(f"can't download {path} from sftp://{self.host}: {e}") from None
        finally:
            sftp.get_channel().get_transport().close()
        return sha.hexdigest()


def private_key(text: str, passphrase: str | None):
    """An SSH private key from its text, whatever its type."""
    import io

    import paramiko

    for kind in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return kind.from_private_key(io.StringIO(text.strip() + "\n"), password=passphrase)
        except paramiko.SSHException:
            continue
    raise SourceError("privateKey in the Source's secret isn't an Ed25519, ECDSA or RSA private key (or its passphrase is wrong)")


# --- the source type

class Files:
    name = "files"
    formats = ["GeoPackage", "Shapefile", "GeoJSON", "FlatGeobuf", "GeoTIFF", "ZIP"]

    def __init__(self, api_key: str | None = None, url: str | None = None, session: requests.Session | None = None,
                 credentials: Callable[[str], str | None] | None = None, options: dict | None = None):
        if not url:
            raise SourceError("a files source needs where the files are: spec.url in the Source, such as /mnt/drop/, "
                              "s3://bucket/prefix/, gs://bucket/prefix/, az://account/container/prefix/, "
                              "sftp://user@host/path/ or ftp://host/path/")
        self.url, options = url, options or {}
        credentials = credentials or (lambda key: None)
        session = session or http_session()
        if is_local_path(url):  # /srv/drop/, D:\\drop\\: a folder, as file:// would be
            self.where = Folder(url)
            return
        parsed = urlparse(url)
        prefix = parsed.path.lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        if parsed.scheme == "file":
            self.where = Folder(parsed.path)
        elif parsed.scheme in ("ftp", "ftps"):
            self.where = Ftp(parsed.hostname or "", parsed.port, prefix, parsed.scheme == "ftps", credentials)
        elif parsed.scheme == "sftp":
            self.where = Sftp(parsed.hostname or "", parsed.port, parsed.username, prefix, credentials, options)
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
        if isinstance(self.where, (Folder, Ftp, Sftp)):
            try:
                return self.where.fetch(download.id, dest)
            except OSError as e:
                raise SourceError(f"can't read {download.id} in {self.url}: {e.strerror or e}") from None
        from ..ingest import download as http_download
        return http_download(self.download_url(download), dest)

