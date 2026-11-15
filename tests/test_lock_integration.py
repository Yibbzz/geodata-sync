"""One `dataops sync` at a time, and what a killed run leaves in staging is cleared up.
Against a real PostGIS (see test_sync_integration.py)."""

import psycopg2
import pytest

from geoserver_dataops import ingest
from test_sync_integration import DB, env, project, schemas, server, sync, tables_in  # noqa: F401 (fixtures)

pytestmark = pytest.mark.integration


def execute(*statements):
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        for sql in statements:
            cur.execute(sql)


def test_a_second_run_at_the_same_time_changes_nothing(tmp_path, server, schemas, env, capsys):
    live, staging, backup = schemas
    root = project(tmp_path, server, schemas)
    db = ingest.Database(host=DB["host"], port=DB["port"], database=DB["dbname"], username=DB["user"],
                         password=DB["password"], live=live, staging=staging, backup=backup)
    held = ingest.hold_lock(db)  # as a run that is loading would
    assert held is not None
    try:
        assert ingest.hold_lock(db) is None
        assert sync(root) == 1
        assert f"another `dataops sync` is loading into this database (staging schema {staging})" in capsys.readouterr().err
        assert tables_in(live) == []
    finally:
        held.close()
    assert sync(root) == 0  # let go when the holder's connection closed
    assert sync(root, "--force") == 0  # and by each run as it ends


def test_another_projects_staging_schema_is_another_lock(schemas):
    live, staging, backup = schemas
    db = ingest.Database(host=DB["host"], port=DB["port"], database=DB["dbname"], username=DB["user"],
                         password=DB["password"], live=live, staging=staging, backup=backup)
    other = ingest.Database(**{**db.__dict__, "staging": staging + "_other"})
    first, second = ingest.hold_lock(db), ingest.hold_lock(other)
    try:
        assert first is not None and second is not None
    finally:
        first.close()
        second.close()


def test_a_killed_runs_staging_tables_are_dropped(tmp_path, server, schemas, env, capsys):
    live, staging, backup = schemas
    orphan = ingest.staging_name("sites_points")
    execute(f"CREATE SCHEMA {staging}", f"CREATE TABLE {staging}.{orphan} (id int)",
            f"CREATE TABLE {staging}.someone_elses (id int)")  # not named as a staging table: left alone
    assert sync(project(tmp_path, server, schemas)) == 0
    assert f"dropped 1 staging table(s) an interrupted run left: {orphan}" in capsys.readouterr().out
    assert tables_in(staging) == ["someone_elses"]
