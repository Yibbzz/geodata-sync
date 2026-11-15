"""`geodata sync` of a raster Dataset, against a real PostGIS (see test_sync_integration.py)."""

import functools
import http.server
import threading

import pytest

from test_sync_integration import env, project, schemas, sync  # noqa: F401 (fixtures)

pytestmark = pytest.mark.integration


def test_an_unchanged_raster_is_not_downloaded_again(tmp_path, schemas, env, capsys, monkeypatch):
    """The source's version (here Last-Modified) is checked before the download, not after it."""
    www, data = tmp_path / "www", tmp_path / "data"
    www.mkdir(), data.mkdir()
    monkeypatch.setenv("GEODATA_DATA_DIR", str(data))
    (www / "dtm.asc").write_text("ncols 2\nnrows 2\nxllcorner 0\nyllcorner 0\ncellsize 50\n1 2\n3 4\n")
    downloads = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if "Range" not in self.headers:  # the version check asks for one byte; a download for the file
                downloads.append(self.path)
            super().do_GET()

        def log_message(self, *args):
            pass

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=str(www)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        root = project(tmp_path, f"http://127.0.0.1:{httpd.server_address[1]}/dtm.asc", schemas,
                       tables="  raster: { path: dtm.tif, crs: 'EPSG:27700' }\n")
        assert sync(root) == 0, capsys.readouterr().err
        assert (data / "dtm.tif").exists() and len(downloads) == 1
        assert sync(root) == 0
        assert "up to date" in capsys.readouterr().out and len(downloads) == 1
        (data / "dtm.tif").unlink()  # the file gone: load it again
        assert sync(root) == 0
        assert (data / "dtm.tif").exists() and len(downloads) == 2
    finally:
        httpd.shutdown()
