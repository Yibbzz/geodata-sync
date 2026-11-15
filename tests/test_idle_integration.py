"""No transaction is left open while a download runs, which can take hours: a server with
idle_in_transaction_session_timeout would end the connection, and an open one holds back vacuum.
Against a real PostGIS (see test_sync_integration.py)."""

import psycopg2.extensions
import pytest

from geoserver_dataops import ingest
from test_sync_integration import env, project, schemas, server, sync, tables_in  # noqa: F401 (fixtures)

pytestmark = pytest.mark.integration


def test_no_transaction_is_open_during_the_download(tmp_path, server, schemas, env, monkeypatch):
    seen = []
    real_download, real_connect = ingest.download, ingest.Database.connect
    connections = []

    def connect(self):
        conn = real_connect(self)
        connections.append(conn)
        return conn

    def statuses():
        seen.append([c.info.transaction_status for c in connections if not c.closed])

    def download(*args, **kwargs):
        statuses()
        return real_download(*args, **kwargs)

    real_file_layers = ingest.file_layers

    def file_layers(*args, **kwargs):  # unzipping and reading a large download takes minutes too
        statuses()
        return real_file_layers(*args, **kwargs)

    monkeypatch.setattr(ingest.Database, "connect", connect)
    monkeypatch.setattr(ingest, "download", download)
    monkeypatch.setattr(ingest, "file_layers", file_layers)
    root = project(tmp_path, server, schemas)
    assert sync(root) == 0  # a first load
    assert sync(root, "--force") == 0  # and one after the last load has been looked up
    assert seen and all(status == psycopg2.extensions.TRANSACTION_STATUS_IDLE for statuses in seen for status in statuses), seen
