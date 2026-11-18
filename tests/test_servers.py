"""The files source on SFTP and FTP servers, and plain paths. The unit tests need nothing; the rest run against
real servers (CI starts them, as .github/workflows/ci.yaml says):

    docker run -d -p 52222:22 atmoz/sftp:alpine drop:dropPass123:1001::upload
    docker run -d -p 52121:21 -p 21000-21010:21000-21010 -e USERS="drop|dropPass123" -e ADDRESS=127.0.0.1 \\
        -e MIN_PORT=21000 -e MAX_PORT=21010 delfer/alpine-ftp-server
    TEST_SFTP_PORT=52222 TEST_FTP_PORT=52121 pytest tests/test_servers.py
"""

import base64
import hashlib
import json
import os
import shutil
import subprocess

import pytest

from geoserver_dataops.sources import SourceError
from geoserver_dataops.sources.files import Files, Sftp, walk_dirs
from test_sync_integration import env, schemas, sync, tables_in  # noqa: F401 (fixtures)

SFTP_PORT, FTP_PORT = os.environ.get("TEST_SFTP_PORT"), os.environ.get("TEST_FTP_PORT")
USER, PASSWORD = "drop", "dropPass123"
needs_gdal = pytest.mark.skipif(shutil.which("ogr2ogr") is None, reason="GDAL's command-line tools not installed")


def creds(**values):
    return lambda key: values.get(key)


def geojson(rows: int) -> bytes:
    return json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"n": i}, "geometry": {"type": "Point", "coordinates": [i, 52]}}
        for i in range(rows)]}).encode()


# --- without a server

def test_a_plain_path_is_a_folder(tmp_path):
    (tmp_path / "roads.geojson").write_bytes(geojson(1))
    assert [p.id for p in Files(url=str(tmp_path) + "/").products().products] == ["roads.geojson"]


def test_sftp_needs_the_servers_key_pinned_and_a_user():
    with pytest.raises(SourceError, match="pin the server's key"):
        Files(url="sftp://drop@files.example/in/", credentials=creds(password="x"))
    with pytest.raises(SourceError, match="give the user"):
        Files(url="sftp://files.example/in/", options={"hostKey": "SHA256:x"})


class Key:
    def __init__(self, blob: bytes, name="ssh-ed25519"):
        self.blob, self.name = blob, name

    def asbytes(self):
        return self.blob

    def get_name(self):
        return self.name

    def get_base64(self):
        return base64.b64encode(self.blob).decode()


def test_the_servers_key_is_checked_against_the_pin():
    key = Key(b"the server's key")
    fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(key.blob).digest()).decode().rstrip("=")
    pinned = Sftp("h", 22, "u", "in/", creds(), {"hostKey": fingerprint})
    assert pinned._trusted(key) and not pinned._trusted(Key(b"another key"))
    known = Sftp("h", 22, "u", "in/", creds(knownHosts=f"# a comment\n[h]:22 ssh-ed25519 {key.get_base64()}\n"), {})
    assert known._trusted(key) and not known._trusted(Key(b"another key"))
    assert not known._trusted(Key(key.blob, name="ssh-rsa"))  # the same bytes as another type of key


def test_a_server_is_walked_folder_by_folder_without_hidden_files():
    tree = {"/in/": [("b.gpkg", False, 10, 1_790_000_000), ("sub", True, None, None), (".hidden", False, 1, 0)],
            "/in/sub/": [("a.zip", False, 20, 1_790_000_100)]}
    entries = walk_dirs("/in/", lambda folder: tree[folder])
    assert [(e.path, e.size, e.modified) for e in entries] == [("b.gpkg", 10, "2026-09-21T14:13:20Z"),
                                                                ("sub/a.zip", 20, "2026-09-21T14:15:00Z")]


# --- against real servers

def upload_sftp(files: dict[str, bytes]):
    import io

    import paramiko
    t = paramiko.Transport(("127.0.0.1", int(SFTP_PORT)))
    t.connect(username=USER, password=PASSWORD)
    s = paramiko.SFTPClient.from_transport(t)
    try:
        s.mkdir("upload/in")
    except OSError:
        pass
    for name, data in files.items():
        s.putfo(io.BytesIO(data), f"upload/in/{name}")
    t.close()


def host_key() -> str:
    """The SFTP server's key fingerprint, as a person pinning it would get it."""
    scan = subprocess.run(["ssh-keyscan", "-t", "ed25519", "-p", SFTP_PORT, "127.0.0.1"], capture_output=True, text=True).stdout
    return subprocess.run(["ssh-keygen", "-lf", "-"], input=scan, capture_output=True, text=True).stdout.split()[1]


@pytest.mark.skipif(not SFTP_PORT or shutil.which("ssh-keyscan") is None, reason="TEST_SFTP_PORT not set (no SFTP server)")
def test_sftp_lists_and_fetches_with_a_pinned_key(tmp_path):
    upload_sftp({"sites.geojson": geojson(3)})
    client = Files(url=f"sftp://{USER}@127.0.0.1:{SFTP_PORT}/upload/in/", credentials=creds(password=PASSWORD),
                   options={"hostKey": host_key()})
    p = client.product("sites.geojson")
    client.fetch(p.chosen, tmp_path / "sites.geojson")
    assert (tmp_path / "sites.geojson").read_bytes() == geojson(3)
    with pytest.raises(SourceError, match="isn't the pinned one"):
        Files(url=f"sftp://{USER}@127.0.0.1:{SFTP_PORT}/upload/in/", credentials=creds(password=PASSWORD),
              options={"hostKey": "SHA256:" + "A" * 43}).products()
    with pytest.raises(SourceError, match="can't log in"):
        Files(url=f"sftp://{USER}@127.0.0.1:{SFTP_PORT}/upload/in/", credentials=creds(password="wrong"),
              options={"hostKey": host_key()}).products()


@pytest.mark.skipif(not FTP_PORT, reason="TEST_FTP_PORT not set (no FTP server)")
def test_ftp_lists_relative_to_the_login_and_fetches(tmp_path):
    import ftplib
    import io
    ftp = ftplib.FTP()
    ftp.connect("127.0.0.1", int(FTP_PORT))
    ftp.login(USER, PASSWORD)
    try:
        ftp.mkd("in")
    except ftplib.error_perm:
        pass
    ftp.storbinary("STOR in/sites.geojson", io.BytesIO(geojson(2)))
    ftp.quit()
    client = Files(url=f"ftp://127.0.0.1:{FTP_PORT}/in/", credentials=creds(username=USER, password=PASSWORD))
    p = client.product("sites.geojson")
    client.fetch(p.chosen, tmp_path / "sites.geojson")
    assert (tmp_path / "sites.geojson").read_bytes() == geojson(2)
    with pytest.raises(SourceError, match="can't log in"):
        Files(url=f"ftp://127.0.0.1:{FTP_PORT}/in/", credentials=creds(username=USER, password="wrong")).products()


@pytest.mark.integration
@needs_gdal
@pytest.mark.skipif(not SFTP_PORT or shutil.which("ssh-keyscan") is None, reason="TEST_SFTP_PORT not set (no SFTP server)")
def test_an_sftp_delivery_loads_and_the_next_into_the_same_table(tmp_path, schemas, env, capsys, monkeypatch):
    live = schemas[0]
    monkeypatch.setenv("DATAOPS_SUPPLIER_PASSWORD", PASSWORD)
    upload_sftp({"parcels_20261101.geojson": geojson(2)})
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "project.yaml").write_text(f"""\
apiVersion: dataops/v1alpha1
kind: Project
metadata: {{ name: t }}
spec:
  database:
    connection: {{ secret: testdb }}
    schemas: {{ live: {live}, staging: {schemas[1]}, backup: {schemas[2]} }}
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: {{ name: supplier }}
spec:
  type: files
  url: sftp://{USER}@127.0.0.1:{SFTP_PORT}/upload/in/
  credentials: {{ secret: supplier }}
  options: {{ hostKey: "{host_key()}" }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec: {{ source: supplier, product: "parcels_*.geojson" }}
""")
    assert sync(tmp_path / "p") == 0, capsys.readouterr().err
    assert tables_in(live) == ["sites_parcels"]
    assert sync(tmp_path / "p") == 0
    assert "sites: up to date (parcels_20261101.geojson" in capsys.readouterr().out
    import time
    time.sleep(1.1)  # a later modification time than the first delivery's
    upload_sftp({"parcels_20261115.geojson": geojson(4)})
    assert sync(tmp_path / "p") == 0
    out = capsys.readouterr().out
    assert "sites: loaded parcels_20261115.geojson" in out and f"{live}.sites_parcels (4 rows)" in out
