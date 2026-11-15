"""The swap of a reloaded table: what the live table had (grants, views) the new one keeps.
Against a real PostGIS (see test_sync_integration.py)."""

import uuid

import psycopg2
import pytest

from test_sync_integration import DB, env, project, query, schemas, server, sync, tables_in  # noqa: F401 (fixtures)

pytestmark = pytest.mark.integration


def execute(*statements):
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        for sql in statements:
            cur.execute(sql)


def test_a_reload_keeps_the_tables_grants_and_views(tmp_path, server, schemas, env, capsys):
    """A view is tied to the table, not its name: unless moved, it follows the old table into the backups."""
    live, staging, backup = schemas
    root = project(tmp_path, server, schemas, backups=0)
    assert sync(root) == 0
    role = f"reader_{uuid.uuid4().hex[:8]}"
    execute(f"CREATE ROLE {role}",
            f"CREATE VIEW {live}.named WITH (security_barrier=true) AS SELECT name, geom FROM {live}.sites_points",
            f"GRANT SELECT ON {live}.sites_points, {live}.named TO {role}")
    try:
        capsys.readouterr()
        assert sync(root, "--force") == 0, capsys.readouterr().err
        grants = "SELECT count(*) FROM information_schema.role_table_grants WHERE grantee = %s AND table_schema = %s AND table_name = %s"
        assert query(grants, role, live, "sites_points") == [(1,)]
        assert query(grants, role, live, "named") == [(1,)]
        definition, options = query("SELECT pg_get_viewdef(c.oid), c.reloptions FROM pg_class c WHERE c.oid = %s::regclass", f"{live}.named")[0]
        assert f"{live}.sites_points" in definition and backup not in definition
        assert options == ["security_barrier=true"]
        assert query(f"SELECT count(*) FROM {live}.named") == [(3,)]
        assert tables_in(backup) == []  # with the view moved, nothing holds the old table back
    finally:
        execute(f"DROP OWNED BY {role}", f"DROP ROLE {role}")


def test_a_materialized_view_stops_the_swap(tmp_path, server, schemas, env, capsys):
    live, staging, backup = schemas
    root = project(tmp_path, server, schemas)
    assert sync(root) == 0
    execute(f"CREATE MATERIALIZED VIEW {live}.summary AS SELECT count(*) FROM {live}.sites_points")
    before = query("SELECT %s::regclass::oid", f"{live}.sites_points")
    capsys.readouterr()
    assert sync(root, "--force") == 1
    assert f"materialized view {live}.summary reads {live}.sites_points" in capsys.readouterr().err
    assert query("SELECT %s::regclass::oid", f"{live}.sites_points") == before
    assert tables_in(staging) == [] and tables_in(backup) == []


def test_a_backup_that_cant_be_dropped_doesnt_fail_the_load(tmp_path, server, schemas, env, capsys):
    live, staging, backup = schemas
    root = project(tmp_path, server, schemas, backups=0)
    assert sync(root) == 0
    execute(f"CREATE TABLE {live}.visits (point integer REFERENCES {live}.sites_points)")  # follows the old table
    capsys.readouterr()
    assert sync(root, "--force") == 0
    assert "was kept: cannot drop table" in capsys.readouterr().out
    assert query("SELECT status FROM public.os_ingest_log WHERE dataset_id = 'sites' ORDER BY id DESC LIMIT 1") == [("success",)]


def test_a_busy_live_table_is_left_alone(tmp_path, server, schemas, env, capsys, monkeypatch):
    """The swap gives up rather than queue behind a long query, which would hold up every other reader."""
    from geodata_sync import ingest
    live, staging, backup = schemas
    root = project(tmp_path, server, schemas)
    assert sync(root) == 0
    monkeypatch.setattr(ingest, "LOCK_TIMEOUT", "200ms")
    monkeypatch.setattr(ingest, "LOCK_ATTEMPTS", 2)
    monkeypatch.setattr(ingest, "LOCK_PAUSE", 0)
    reader = psycopg2.connect(**DB)
    try:
        with reader.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {live}.sites_points")  # an open transaction holding a read lock
        capsys.readouterr()
        assert sync(root, "--force") == 1
        assert "stayed in use" in capsys.readouterr().err
        assert tables_in(staging) == [] and tables_in(backup) == []
    finally:
        reader.close()
    assert sync(root, "--force") == 0
