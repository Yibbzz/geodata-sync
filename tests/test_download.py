"""Downloads (ingest.download): a connection lost part way is tried again, from where it stopped if it can be."""

import hashlib
import http.server
import threading

import pytest
import requests

from geodata_sync import ingest

DATA = bytes(range(256)) * 4096 * 4  # 4 MiB: it drops after 2, a whole read of 1 MiB or more


class Flaky(http.server.BaseHTTPRequestHandler):
    """Serves DATA; the first `drops` responses stop half way. Sends parts of it only if `ranges`."""

    drops, ranges, requests_seen = 1, True, []

    def do_GET(self):
        type(self).requests_seen.append(self.headers.get("Range"))
        start = 0
        if self.ranges and self.headers.get("Range"):
            start = int(self.headers["Range"].removeprefix("bytes=").rstrip("-"))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(DATA) - 1}/{len(DATA)}")
        else:
            self.send_response(200)
        body = DATA[start:]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if type(self).drops > 0:
            type(self).drops -= 1
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            self.connection.shutdown(2)  # the connection lost half way
            return
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def serve(monkeypatch):
    monkeypatch.setattr(ingest, "DOWNLOAD_PAUSE", 0)
    servers = []

    def start(drops, ranges):
        handler = type("Handler", (Flaky,), {"drops": drops, "ranges": ranges, "requests_seen": []})
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        servers.append(httpd)
        return f"http://127.0.0.1:{httpd.server_address[1]}/f.zip", handler

    yield start
    for httpd in servers:
        httpd.shutdown()


def test_a_dropped_download_resumes_where_it_stopped(serve, tmp_path):
    url, handler = serve(drops=1, ranges=True)
    sha = ingest.download(url, tmp_path / "f", md5=hashlib.md5(DATA).hexdigest())
    assert (tmp_path / "f").read_bytes() == DATA and sha == hashlib.sha256(DATA).hexdigest()
    assert handler.requests_seen[0] is None and handler.requests_seen[1].startswith("bytes=") \
        and handler.requests_seen[1] != "bytes=0-"


def test_a_server_without_ranges_is_downloaded_again_from_the_start(serve, tmp_path):
    url, handler = serve(drops=2, ranges=False)
    sha = ingest.download(url, tmp_path / "f", md5=hashlib.md5(DATA).hexdigest())
    assert (tmp_path / "f").read_bytes() == DATA and sha == hashlib.sha256(DATA).hexdigest()
    assert len(handler.requests_seen) == 3


def test_it_gives_up_in_the_end(serve, tmp_path):
    url, _ = serve(drops=ingest.DOWNLOAD_ATTEMPTS, ranges=True)
    with pytest.raises(requests.RequestException):
        ingest.download(url, tmp_path / "f")
