"""Spreadsheets as data (geoserver_dataops/spreadsheet.py): CSV, Excel and ODS, their rows points by their
coordinate columns."""

import shutil
import subprocess

import psycopg2
import pytest

from geoserver_dataops.cli import main
from geoserver_dataops.sources.formats import classify, rank
from geoserver_dataops.spreadsheet import points, sheets_to_csv
from test_sync_integration import DB, env, schemas  # noqa: F401 (fixtures)

needs_gdal = pytest.mark.skipif(shutil.which("ogr2ogr") is None, reason="GDAL's command-line tools not installed")


def csv(tmp_path, text: str, name: str = "sites.csv"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_coordinate_columns_are_found_by_name_whatever_their_case(tmp_path):
    options, crs, _ = points(csv(tmp_path, "name,Latitude,Longitude\nA,52.6,-0.7\n"), None, "EPSG:27700")
    assert ("X_POSSIBLE_NAMES=Longitude" in options, "Y_POSSIBLE_NAMES=Latitude" in options, crs) == (True, True, "EPSG:4326")
    options, crs, _ = points(csv(tmp_path, "﻿name,EASTING,NORTHING\nA,480000,300000\n"), None, "EPSG:27700")
    assert ("X_POSSIBLE_NAMES=EASTING" in options, crs) == (True, "EPSG:27700")  # the project's, after a byte-order mark


def test_latitude_and_longitude_win_over_x_and_y(tmp_path):
    options, crs, _ = points(csv(tmp_path, "X,Y,Latitude,Longitude\n1,2,40.7,-73.9\n"), None, "EPSG:27700")
    assert "X_POSSIBLE_NAMES=Longitude" in options and crs == "EPSG:4326"


def test_a_dataset_names_its_columns_and_crs(tmp_path):
    path = csv(tmp_path, "id,E,N,LAT,LONG\n1,480000,300000,52.6,-0.7\n")
    options, crs, _ = points(path, {"x": "e", "y": "n"}, "EPSG:27700")
    assert ("X_POSSIBLE_NAMES=E" in options, crs) == (True, "EPSG:27700")
    assert points(path, {"x": "E", "y": "N", "crs": "EPSG:3857"}, None)[1] == "EPSG:3857"
    with pytest.raises(ValueError, match="no column 'east'"):
        points(path, {"x": "east", "y": "N"}, None)


def test_a_wkt_column_and_no_coordinates_at_all(tmp_path):
    options, crs, _ = points(csv(tmp_path, 'id,WKT\n1,"POINT (1 2)"\n'), None, "EPSG:27700")
    assert "GEOM_POSSIBLE_NAMES=WKT" in options and crs == "EPSG:4326"
    options, crs, note = points(csv(tmp_path, "team,phone\nnorth,1\n", "contacts.csv"), None, "EPSG:27700")
    assert (crs, "without geometry" in note) == (None, True)


def test_spreadsheets_are_loadable_and_ranked_below_gis_formats():
    assert [classify("CSV"), classify("xlsx"), classify(None, "https://x/sheet.ods")] == ["CSV", "Excel", "ODS"]
    assert rank("GeoPackage") < rank("ZIP") < rank("CSV")


@needs_gdal
def test_each_sheet_of_a_workbook_becomes_a_csv_with_its_header(tmp_path):
    src = csv(tmp_path, "name,lat,lon\nA,52.6,-0.7\nB,52.7,-0.8\n")
    book = tmp_path / "sites.xlsx"
    subprocess.run(["ogr2ogr", "-f", "XLSX", str(book), str(src), "-nln", "North"], check=True, capture_output=True)
    subprocess.run(["ogr2ogr", "-f", "XLSX", "-update", str(book), str(src), "-nln", "South"], check=True, capture_output=True)
    made = sheets_to_csv(book, tmp_path / "out")
    assert sorted(p.name for p in made) == ["North.csv", "South.csv"]
    assert made[0].read_text().splitlines()[0].replace('"', "") == "name,lat,lon"  # a sheet of text: its first row the header


@pytest.mark.integration
@needs_gdal
def test_a_csv_and_a_workbook_load_as_points(tmp_path, schemas, env, capsys):
    live = schemas[0]
    drop = tmp_path / "drop"
    drop.mkdir()
    csv(drop, "name,Easting,Northing\nA,480000,300000\nB,480100,300100\n", "valves.csv")
    src = csv(tmp_path, "name,lat,lon\nA,52.6,-0.7\n")
    subprocess.run(["ogr2ogr", "-f", "XLSX", str(drop / "hydrants.xlsx"), str(src), "-nln", "Hydrants"], check=True, capture_output=True)
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "project.yaml").write_text(f"""\
apiVersion: dataops/v1alpha1
kind: Project
metadata: {{ name: t }}
spec:
  database:
    connection: {{ secret: testdb }}
    schemas: {{ live: {live}, staging: {schemas[1]}, backup: {schemas[2]} }}
  crs: EPSG:27700
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: {{ name: drop }}
spec: {{ type: files, url: "{drop}/" }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: valves }}
spec: {{ source: drop, product: valves.csv }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: hydrants }}
spec: {{ source: drop, product: hydrants.xlsx }}
""")
    assert main(["sync", "-C", str(tmp_path / "p"), "valves", "hydrants"]) == 0, capsys.readouterr().err
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        cur.execute(f"SELECT count(geom), max(ST_SRID(geom)), max(name) FROM {live}.valves_valves")
        assert cur.fetchone() == (2, 27700, "B")
        cur.execute(f"SELECT count(geom), max(ST_SRID(geom)), round(max(ST_Y(ST_GeometryN(geom, 1)))::numeric, 1)::float FROM {live}.hydrants_hydrants")
        assert cur.fetchone() == (1, 4326, 52.6)
