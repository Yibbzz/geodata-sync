"""`geodata sync` over several datasets: one dataset's failure, whatever it is, doesn't stop the others.
Against a real PostGIS (see test_sync_integration.py)."""

import psycopg2
import pytest

from geodata_sync import sync as sync_module
from geodata_sync.cli import main
from test_sync_integration import DB, env, query, schemas, server, tables_in  # noqa: F401 (fixtures)

pytestmark = pytest.mark.integration


def two_datasets(tmp_path, url, schemas):
    """`broken` (sorted first) and `sites`, both from `url`."""
    datasets = "".join(f"""---
apiVersion: geodata/v1alpha1
kind: Dataset
metadata: {{ name: {name} }}
spec: {{ source: web, url: "{url}" }}
""" for name in ("broken", "sites"))
    (tmp_path / "project.yaml").write_text(f"""\
apiVersion: geodata/v1alpha1
kind: Project
metadata: {{ name: t }}
spec:
  database:
    connection: {{ secret: testdb }}
    schemas: {{ live: {schemas[0]}, staging: {schemas[1]}, backup: {schemas[2]} }}
---
apiVersion: geodata/v1alpha1
kind: Source
metadata: {{ name: web }}
spec: {{ type: url }}
{datasets}""")
    return tmp_path


@pytest.fixture
def broken_log():
    yield
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM public.os_ingest_log WHERE dataset_id = 'broken'")


def failing(monkeypatch, fail):
    """sync_dataset, but `fail(conn)` runs instead for the dataset named broken."""
    real = sync_module.sync_dataset

    def fake(conn, db, root, project, dataset, force):
        if dataset.name == "broken":
            fail(conn)
        return real(conn, db, root, project, dataset, force)

    monkeypatch.setattr(sync_module, "sync_dataset", fake)


def last_status(dataset):
    return query("SELECT status, error FROM public.os_ingest_log WHERE dataset_id = %s ORDER BY id DESC LIMIT 1", dataset)


def test_an_unexpected_error_fails_only_its_dataset(tmp_path, server, schemas, env, broken_log, capsys, monkeypatch):
    def fail(conn):
        raise KeyError("id")  # a source answering in a shape nobody expected

    failing(monkeypatch, fail)
    assert main(["sync", "-C", str(two_datasets(tmp_path, server, schemas)), "broken", "sites"]) == 1
    out = capsys.readouterr()
    assert "broken: failed: unexpected KeyError: 'id' (please report this)" in out.err
    assert "sites: loaded" in out.out and "sites_points" in tables_in(schemas[0])
    assert last_status("broken")[0][0] == "failed"


def test_a_download_that_fails_mid_run_fails_only_its_dataset(tmp_path, server, schemas, env, broken_log, capsys, monkeypatch):
    """The file is gone by the time it's downloaded (requests' HTTPError, an OSError)."""
    from geodata_sync.sources.url import URL
    real = URL.download_url
    monkeypatch.setattr(URL, "download_url", lambda self, d: real(self, d).replace("sites.zip?gone", "missing.zip"))
    root = two_datasets(tmp_path, server, schemas)
    text = (root / "project.yaml").read_text()
    (root / "project.yaml").write_text(text.replace('{ name: broken }\nspec: { source: web, url: "' + server,
                                                    '{ name: broken }\nspec: { source: web, url: "' + server + "?gone"))
    assert main(["sync", "-C", str(root), "broken", "sites"]) == 1
    out = capsys.readouterr()
    assert "broken: failed: 404 Client Error" in out.err
    assert "sites: loaded" in out.out
    status, error = last_status("broken")[0]
    assert status == "failed" and "404" in error


def test_a_lost_connection_is_made_again(tmp_path, server, schemas, env, broken_log, capsys, monkeypatch):
    def fail(conn):
        with psycopg2.connect(**DB) as other, other.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(%s)", (conn.get_backend_pid(),))
        with conn.cursor() as cur:
            cur.execute("SELECT 1")  # the server has gone: OperationalError, and the connection is closed

    failing(monkeypatch, fail)
    assert main(["sync", "-C", str(two_datasets(tmp_path, server, schemas)), "broken", "sites"]) == 1
    out = capsys.readouterr()
    assert "broken: failed: " in out.err and "sites: loaded" in out.out
    assert last_status("broken")[0][0] == "failed" and last_status("sites")[0][0] == "success"


def test_no_database_at_all_marks_the_rest_not_checked(tmp_path, server, schemas, env, broken_log, capsys, monkeypatch):
    def fail(conn):
        conn.close()
        raise psycopg2.OperationalError("server closed the connection unexpectedly")

    failing(monkeypatch, fail)
    monkeypatch.setattr(sync_module.ingest.Database, "connect",
                        lambda self: (_ for _ in ()).throw(psycopg2.OperationalError("refused")), raising=True)
    monkeypatch.setattr(sync_module, "database", lambda project, root: _connected_once(project, root))
    assert main(["sync", "-C", str(two_datasets(tmp_path, server, schemas)), "broken", "sites"]) == 1
    err = capsys.readouterr().err
    assert "broken: failed: server closed the connection unexpectedly" in err
    assert "sites: not checked: lost the connection to the database" in err


def _connected_once(project, root, real=sync_module.database):
    """The project's database, which can be connected to once: the first connect works, later ones don't."""
    db = real(project, root)
    first = psycopg2.connect(host=db.host, port=db.port, dbname=db.database, user=db.username, password=db.password)

    class Once(type(db)):
        used = False

        def connect(self):
            if Once.used:
                raise psycopg2.OperationalError("refused")
            Once.used = True
            return first

    return Once(**db.__dict__)
