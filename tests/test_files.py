"""The files source type: folders, S3 and Azure (against fakes), and AWS's own signing example."""

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone

import pytest

from geoserver_dataops.sources import NotFound, SourceError
from geoserver_dataops.sources.files import Files, presign
from geoserver_dataops.patterns import pattern_stem
from fake_buckets import AzureServer, S3Server
from test_sync_integration import env, schemas, sync, tables_in  # noqa: F401 (fixtures)

needs_gdal = pytest.mark.skipif(shutil.which("ogr2ogr") is None, reason="GDAL's command-line tools not installed")


def geojson(rows: int) -> bytes:
    return json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"n": i}, "geometry": {"type": "Point", "coordinates": [i, 52]}}
        for i in range(rows)]}).encode()


def test_signing_matches_awss_own_example():
    """AWS's documented presigned URL: GET examplebucket's test.txt, 24 hours, 24 May 2013."""
    url = presign("GET", "https://examplebucket.s3.amazonaws.com/test.txt", {}, "us-east-1",
                  "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", None,
                  now=datetime(2013, 5, 24, tzinfo=timezone.utc), expires=86400)
    assert url.endswith("X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")
    assert "X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request" in url


# --- a folder

def test_a_folders_loadable_files_are_the_products(tmp_path):
    (tmp_path / "2026").mkdir()
    (tmp_path / "2026" / "roads.geojson").write_bytes(geojson(2))
    (tmp_path / "notes.txt").write_text("not data")
    (tmp_path / ".hidden.gpkg").write_bytes(b"")
    listing = Files(url=tmp_path.as_uri() + "/").products()
    assert [(p.id, p.loads_as) for p in listing.products] == [("2026/roads.geojson", "GeoJSON")]


def test_a_pattern_is_the_newest_matching_file(tmp_path):
    for name, age in (("roads_20261101.geojson", 200), ("roads_20261115.geojson", 100), ("rivers_20261120.geojson", 0)):
        (tmp_path / name).write_bytes(geojson(1))
        os.utime(tmp_path / name, (time.time() - age, time.time() - age))
    p = Files(url=tmp_path.as_uri()).product("roads_*.geojson")
    assert (p.id, p.chosen.id) == ("roads_*.geojson", "roads_20261115.geojson")
    assert p.version.startswith("roads_20261115.geojson ")  # which delivery shows in sync's output
    with pytest.raises(NotFound):
        Files(url=tmp_path.as_uri()).product("lakes_*.geojson")


def test_a_files_version_changes_when_it_is_replaced(tmp_path):
    (tmp_path / "a.geojson").write_bytes(geojson(1))
    before = Files(url=tmp_path.as_uri()).product("a.geojson").version
    (tmp_path / "a.geojson").write_bytes(geojson(2))
    assert Files(url=tmp_path.as_uri()).product("a.geojson").version != before


def test_a_folder_that_isnt_there_says_so(tmp_path):
    with pytest.raises(SourceError, match="is the volume or share mounted"):
        Files(url=(tmp_path / "missing").as_uri())
    with pytest.raises(SourceError, match="reads file://, s3://, gs://, az://, sftp://, ftp://, ftps://"):
        Files(url="webdav://example.org/x/")


def test_a_pattern_names_the_table_not_the_delivery():
    assert pattern_stem("roads_*.zip") == "roads"
    assert pattern_stem("deliveries/2026-??-*_parcels.gpkg") == "2026_parcels"
    assert pattern_stem("*.zip") == "data"


# --- a bucket

@pytest.fixture
def s3():
    s = S3Server(needs_key="key1")
    yield s
    s.close()


def bucket(s3, **creds):
    return Files(url="s3://drop/in/", options={"endpoint": s3.url},
                 credentials=lambda k: {"accessKeyId": "key1", "secretAccessKey": "secret1", **creds}.get(k))


def test_a_bucket_is_listed_page_by_page_and_signed(s3):
    for i in range(5):
        s3.put(f"in/sites_{i}.geojson", geojson(i + 1))
    s3.put("in/readme.txt", b"x")
    s3.put("elsewhere/x.geojson", geojson(1))
    listing = bucket(s3).products()
    assert [p.id for p in listing.products] == [f"sites_{i}.geojson" for i in range(5)]
    assert sum("prefix=in" in r for r in s3.requests) >= 3  # two per page: several pages
    assert all("X-Amz-Signature=" in r for r in s3.requests)


def test_a_bucket_file_is_fetched_through_a_signed_address(s3, tmp_path):
    s3.put("in/sites.geojson", geojson(3))
    client = bucket(s3)
    p = client.product("sites.geojson")
    assert "X-Amz-Signature=" in client.download_url(p.chosen)
    client.fetch(p.chosen, tmp_path / "sites.geojson")
    assert (tmp_path / "sites.geojson").read_bytes() == geojson(3)


def test_a_bucket_that_refuses_says_what_is_missing(s3):
    s3.put("in/a.geojson", geojson(1))
    with pytest.raises(SourceError, match="no credentials: set accessKeyId and secretAccessKey"):
        Files(url="s3://drop/in/", options={"endpoint": s3.url}).products()
    with pytest.raises(SourceError, match="refused access"):
        bucket(s3, accessKeyId="someone-else").products()


def test_aws_tells_the_buckets_region_and_it_is_asked_there(monkeypatch):
    """Without a region, a bucket in another one answers 301 with x-amz-bucket-region: asked again there."""
    from geoserver_dataops.sources import files
    s3 = S3Server()
    try:
        s3.put("in/a.geojson", geojson(1))
        client = Files(url="s3://drop/in/")
        client.where.base = f"{s3.url}/drop"  # as AWS's address, but local
        s3.wrong_region = True
        asked = []
        monkeypatch.setattr(files.Bucket, "_get", lambda self, key="", params=None, real=files.Bucket._get:
                            (asked.append(self.region), setattr(s3, "wrong_region", self.region != "eu-west-2"),
                             setattr(self, "base", f"{s3.url}/drop"), real(self, key, params))[-1])
        assert [p.id for p in client.products().products] == ["a.geojson"]
        assert asked[:2] == ["us-east-1", "eu-west-2"]
    finally:
        s3.close()


# --- Azure

def test_an_azure_container_is_listed_and_read_with_its_sas_token(tmp_path):
    az = AzureServer()
    try:
        for i in range(3):
            az.put(f"exports/parcels_{i}.geojson", geojson(1), modified=1_790_000_000 + i)
        url = "az://acct/maps/exports/"
        client = Files(url=url, options={"endpoint": az.url}, credentials=lambda k: "sv=1&sig=ok" if k == "sasToken" else None)
        p = client.product("parcels_*.geojson")
        assert p.chosen.id == "parcels_2.geojson" and p.version.startswith("parcels_2.geojson 2026-")
        client.fetch(p.chosen, tmp_path / "p.geojson")
        assert (tmp_path / "p.geojson").read_bytes() == geojson(1)
        with pytest.raises(SourceError, match="no sasToken"):
            Files(url=url, options={"endpoint": az.url}).products()
    finally:
        az.close()


# --- loading

@pytest.mark.integration
@needs_gdal
def test_each_new_delivery_loads_into_the_same_table(tmp_path, schemas, env, capsys):
    live = schemas[0]
    drop = tmp_path / "drop"
    drop.mkdir()
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
metadata: {{ name: share }}
spec: {{ type: files, url: "{drop.as_uri()}/" }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec: {{ source: share, product: "roads_*.zip" }}
""")

    def deliver(day: str, rows: int, age: float):
        work = tmp_path / day
        work.mkdir()
        (tmp_path / f"{day}.geojson").write_bytes(geojson(rows))  # beside the folder that is zipped
        subprocess.run(["ogr2ogr", "-f", "ESRI Shapefile", str(work / f"roads_{day}.shp"), str(tmp_path / f"{day}.geojson")],
                       check=True, capture_output=True)
        shutil.make_archive(str(drop / f"roads_{day}"), "zip", work, None)
        os.utime(drop / f"roads_{day}.zip", (time.time() - age, time.time() - age))

    deliver("20261101", 2, 100)
    assert sync(tmp_path / "p") == 0, capsys.readouterr().err
    assert tables_in(live) == ["sites_roads"]
    assert sync(tmp_path / "p") == 0
    assert "sites: up to date (roads_20261101.zip" in capsys.readouterr().out

    deliver("20261115", 3, 0)  # the next delivery, another file name: the same table
    assert sync(tmp_path / "p") == 0
    out = capsys.readouterr().out
    assert "sites: loaded roads_20261115.zip" in out and f"{live}.sites_roads (3 rows)" in out
    assert tables_in(live) == ["sites_roads"]
