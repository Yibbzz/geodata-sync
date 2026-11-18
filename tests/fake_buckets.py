"""A small S3 and a small Azure Blob Storage on local ports, for the files source's tests. Each lists its
objects a page at a time (two per page) and serves them. The S3 one can ask for credentials, and can
answer as a bucket in another region (301 with x-amz-bucket-region) until asked at the right address."""

import http.server
import threading
from email.utils import formatdate
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

PAGE = 2


class Store:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, float]] = {}  # key -> (data, modified)
        self.requests: list[str] = []

    def put(self, key: str, data: bytes, modified: float = 1_790_000_000):
        self.objects[key] = (data, modified)

    def page(self, prefix: str, marker: str) -> tuple[list[str], bool]:
        keys = sorted(k for k in self.objects if k.startswith(prefix) and k > marker)
        return keys[:PAGE], len(keys) > PAGE


class Server:
    def __init__(self, handler):
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()


def reply(handler, status, body=b"", headers=None):
    handler.send_response(status)
    for k, v in {"Content-Length": str(len(body)), **(headers or {})}.items():
        handler.send_header(k, v)
    handler.end_headers()
    handler.wfile.write(body)


class S3Server(Server, Store):
    """Path-style S3 (bucket `drop`), as MinIO answers."""

    def __init__(self, needs_key: str | None = None):
        Store.__init__(self)
        self.needs_key, self.wrong_region = needs_key, False
        store = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                store.requests.append(self.path)
                if store.wrong_region:
                    return reply(self, 301, b"<Error><Code>PermanentRedirect</Code></Error>", {"x-amz-bucket-region": "eu-west-2"})
                if store.needs_key and not query.get("X-Amz-Credential", "").startswith(f"{store.needs_key}/"):
                    return reply(self, 403, b"<Error><Code>AccessDenied</Code></Error>")
                bucket, _, key = url.path.lstrip("/").partition("/")
                if bucket != "drop":
                    return reply(self, 404, b"<Error><Code>NoSuchBucket</Code></Error>")
                if not key:
                    keys, more = store.page(query.get("prefix", ""), query.get("marker", ""))
                    items = "".join(
                        f"<Contents><Key>{escape(k)}</Key><Size>{len(store.objects[k][0])}</Size>"
                        f"<LastModified>2026-11-0{1 + int(store.objects[k][1]) % 9}T10:00:00.000Z</LastModified>"
                        f"<ETag>\"{abs(hash(store.objects[k][0])):x}\"</ETag></Contents>" for k in keys)
                    body = (f'<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>drop</Name>'
                            f"<IsTruncated>{'true' if more else 'false'}</IsTruncated>{items}</ListBucketResult>")
                    return reply(self, 200, body.encode(), {"Content-Type": "application/xml"})
                if key not in store.objects:
                    return reply(self, 404)
                reply(self, 200, store.objects[key][0])

            def log_message(self, *args):
                pass

        Server.__init__(self, Handler)


class AzureServer(Server, Store):
    """Azure Blob Storage's List Blobs (container `maps`), which refuses a request without the SAS token
    `sig=ok` with 409, as a container closed to the public does."""

    def __init__(self):
        Store.__init__(self)
        store = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                store.requests.append(self.path)
                if query.get("sig") != "ok":
                    return reply(self, 409, b"<Error><Code>PublicAccessNotPermitted</Code></Error>")
                container, _, name = url.path.lstrip("/").partition("/")
                if container != "maps":
                    return reply(self, 404)
                if query.get("comp") == "list":
                    names, more = store.page(query.get("prefix", ""), query.get("marker", ""))
                    blobs = "".join(
                        f"<Blob><Name>{escape(n)}</Name><Properties><Last-Modified>{formatdate(store.objects[n][1], usegmt=True)}"
                        f"</Last-Modified><Etag>0x{abs(hash(store.objects[n][0])):X}</Etag>"
                        f"<Content-Length>{len(store.objects[n][0])}</Content-Length></Properties></Blob>" for n in names)
                    marker = f"<NextMarker>{escape(names[-1])}</NextMarker>" if more else "<NextMarker/>"
                    body = f"<EnumerationResults><Blobs>{blobs}</Blobs>{marker}</EnumerationResults>"
                    return reply(self, 200, body.encode(), {"Content-Type": "application/xml"})
                if name not in store.objects:
                    return reply(self, 404)
                reply(self, 200, store.objects[name][0])

            def log_message(self, *args):
                pass

        Server.__init__(self, Handler)
