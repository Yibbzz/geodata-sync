"""The database source type: tables in another PostgreSQL or MySQL/MariaDB database. The integration tests use the
test PostGIS itself as the source (another schema), and a MariaDB when TEST_MYSQL_PORT says where one is:

    docker run -d -p 53306:3306 -e MARIADB_ROOT_PASSWORD=rootpw -e MARIADB_DATABASE=assets mariadb:11.8
"""

import os
import shutil
import uuid

import psycopg2
import pytest

from geoserver_dataops.cli import main
from geoserver_dataops.loader import load
from geoserver_dataops.secrets import redact
from geoserver_dataops.sources import SourceError
from geoserver_dataops.sources.database import Database
from geoserver_dataops.validate import validate
from test_sync_integration import DB, env, schemas, tables_in  # noqa: F401 (fixtures)

needs_gdal = pytest.mark.skipif(shutil.which("ogr2ogr") is None, reason="GDAL's command-line tools not installed")
MYSQL_PORT = os.environ.get("TEST_MYSQL_PORT")


def creds(**values):
    return lambda key: values.get(key)


def test_a_postgresql_address_becomes_gdals_connection_with_the_secrets_login():
    db = Database(url="postgresql://db.example.org/gis?sslmode=require", credentials=creds(username="u", password="it's a secret"))
    assert db.connection() == "PG:host=db.example.org port=5432 dbname=gis user=u password='it\\'s a secret' sslmode=require"
    assert db.url == "postgresql://db.example.org:5432/gis"  # as shown: no login


def test_a_mysql_address_and_a_user_in_it():
    db = Database(url="mysql://reader@db.example.org/assets", credentials=creds(password="pw"))
    assert db.connection() == "MYSQL:assets,host=db.example.org,port=3306,user=reader,password=pw"


def test_what_isnt_read_says_so():
    with pytest.raises(SourceError, match="reads postgresql:// and mysql://"):
        Database(url="mssql://db.example.org/gis")
    with pytest.raises(SourceError, match="name the database"):
        Database(url="postgresql://db.example.org/")


def test_a_connections_password_is_masked_in_messages():
    assert redact("unable to open 'PG:host=h user=u password='a b' dbname=x'") == "unable to open 'PG:host=h user=u password=*** dbname=x'"
    assert redact("MYSQL:gis,host=h,user=u,password=pw,port=3306") == "MYSQL:gis,host=h,user=u,password=***,port=3306"


def test_validate_wants_a_database_address(tmp_path):
    (tmp_path / "p.yaml").write_text("""\
apiVersion: dataops/v1alpha1
kind: Project
metadata: { name: t }
spec: { database: { connection: { secret: db } } }
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: { name: assets }
spec: { type: database, url: "https://db.example.org/gis" }
""")
    assert any("postgresql://host:5432/dbname" in p.message for p in validate(load(tmp_path)))


@pytest.fixture
def source_table():
    """A schema in the test PostGIS standing for another organisation's database: a table and a view."""
    schema = f"assets_{uuid.uuid4().hex[:8]}"
    write(f"CREATE SCHEMA {schema}",
          f"CREATE TABLE {schema}.valves (id serial PRIMARY KEY, name text, geom geometry(Point, 27700))",
          f"INSERT INTO {schema}.valves (name, geom) SELECT 'v' || g, ST_SetSRID(ST_MakePoint(480000 + g, 300000), 27700) "
          "FROM generate_series(1, 5) g",
          f"CREATE VIEW {schema}.some_valves AS SELECT * FROM {schema}.valves WHERE id <= 2")
    yield schema
    write(f"DROP SCHEMA {schema} CASCADE")


def write(*statements: str) -> None:
    """Run and close: PostgreSQL counts a session's changes in its statistics when the session ends (or idles)."""
    conn = psycopg2.connect(**DB)
    try:
        with conn, conn.cursor() as cur:
            for sql in statements:
                cur.execute(sql)
    finally:
        conn.close()


def project(tmp_path, schemas, source: str, product: str) -> object:
    (tmp_path / "p").mkdir(exist_ok=True)
    (tmp_path / "p" / "project.yaml").write_text(f"""\
apiVersion: dataops/v1alpha1
kind: Project
metadata: {{ name: t }}
spec:
  database:
    connection: {{ secret: testdb }}
    schemas: {{ live: {schemas[0]}, staging: {schemas[1]}, backup: {schemas[2]} }}
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: {{ name: assets }}
spec: {{ type: database, url: "{source}", credentials: {{ secret: assets }} }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec: {{ source: assets, product: "{product}", tables: {{ "{product}": {{ name: copied }} }} }}
""")
    return tmp_path / "p"


def sync(root, capsys) -> str:
    assert main(["sync", "-C", str(root), "sites"]) == 0, capsys.readouterr().err
    return capsys.readouterr().out


@pytest.mark.integration
@needs_gdal
def test_a_postgresql_table_is_copied_and_again_only_when_it_changes(tmp_path, schemas, env, capsys, monkeypatch, source_table):
    monkeypatch.setenv("DATAOPS_ASSETS_USERNAME", DB["user"])
    monkeypatch.setenv("DATAOPS_ASSETS_PASSWORD", DB["password"])
    root = project(tmp_path, schemas, f"postgresql://{DB['host']}:{DB['port']}/{DB['dbname']}", f"{source_table}.valves")
    assert "sites: loaded changes 5+0+0" in sync(root, capsys)
    assert tables_in(schemas[0]) == ["copied"]
    assert "sites: up to date (changes 5+0+0)" in sync(root, capsys)
    write(f"UPDATE {source_table}.valves SET name = 'changed' WHERE id = 1")
    assert "sites: loaded changes 5+1+0" in sync(root, capsys)


@pytest.mark.integration
@needs_gdal
def test_a_view_is_copied_each_time(tmp_path, schemas, env, capsys, monkeypatch, source_table):
    monkeypatch.setenv("DATAOPS_ASSETS_USERNAME", DB["user"])
    monkeypatch.setenv("DATAOPS_ASSETS_PASSWORD", DB["password"])
    root = project(tmp_path, schemas, f"postgresql://{DB['host']}:{DB['port']}/{DB['dbname']}", f"{source_table}.some_valves")
    assert "(2 rows)" in sync(root, capsys)
    assert "sites: loaded" in sync(root, capsys)  # no version: copied each time it is due


@pytest.mark.integration
@needs_gdal
@pytest.mark.skipif(not MYSQL_PORT, reason="TEST_MYSQL_PORT not set (no MariaDB)")
def test_a_mariadb_table_is_copied(tmp_path, schemas, env, capsys, monkeypatch):
    import subprocess
    connection = f"MYSQL:assets,host=127.0.0.1,port={MYSQL_PORT},user=root,password=rootpw"
    for sql in ("CREATE TABLE IF NOT EXISTS hydrants (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(50), geom POINT NOT NULL)",
                "DELETE FROM hydrants",
                "INSERT INTO hydrants (name, geom) VALUES ('h1', ST_GeomFromText('POINT(-0.72 52.67)', 4326)), "
                "('h2', ST_GeomFromText('POINT(-0.73 52.68)', 4326))"):  # GDAL runs one statement at a time
        subprocess.run(["ogrinfo", "-if", "MySQL", connection, "-sql", sql], check=True, capture_output=True)
    monkeypatch.setenv("DATAOPS_ASSETS_USERNAME", "root")
    monkeypatch.setenv("DATAOPS_ASSETS_PASSWORD", "rootpw")
    root = project(tmp_path, schemas, f"mysql://127.0.0.1:{MYSQL_PORT}/assets", "hydrants")
    assert "(2 rows)" in sync(root, capsys)
    monkeypatch.setenv("DATAOPS_ASSETS_PASSWORD", "wrong")
    assert main(["discover", "assets", "-C", str(root)]) != 0
    assert "access denied for user" in capsys.readouterr().err
