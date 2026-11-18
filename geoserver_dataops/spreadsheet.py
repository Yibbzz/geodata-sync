"""Spreadsheets as data: CSV, and Excel and LibreOffice sheets, whose rows are points by their coordinate columns.

Each sheet of a workbook (.xlsx, .xls, .ods) is made a CSV first, so one rule reads them all: GDAL's CSV reader,
told which columns hold the coordinates. They are found by name, whatever their case: lon, lng, longitude and
lat, latitude (taken as WGS84); easting, northing, x, y (taken as the project's CRS); or a WKT column (wkt,
geometry, the_geom). A Dataset's `points` names them instead ({ x, y, crs } or { wkt, crs }). A sheet with no
coordinate columns loads as a table without geometry, which GeoServer can still join to.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
from pathlib import Path

CSV_SUFFIXES = (".csv", ".tsv")
SHEET_SUFFIXES = (".xlsx", ".xls", ".ods")
X_NAMES = ("longitude", "lon", "long", "lng", "easting", "east", "x", "x_coord", "xcoord")
Y_NAMES = ("latitude", "lat", "northing", "north", "y", "y_coord", "ycoord")
WKT_NAMES = ("wkt", "geometry", "the_geom", "geom")
GEOGRAPHIC = {"longitude", "lon", "long", "lng", "latitude", "lat"}
DEFAULT_OPEN = [f"X_POSSIBLE_NAMES={','.join(X_NAMES)}", f"Y_POSSIBLE_NAMES={','.join(Y_NAMES)}",
                f"GEOM_POSSIBLE_NAMES={','.join(WKT_NAMES)}", "KEEP_GEOM_COLUMNS=NO", "AUTODETECT_TYPE=YES"]


def is_csv(path: str) -> bool:
    return path.lower().endswith(CSV_SUFFIXES)


def is_sheet(path: str) -> bool:
    return path.lower().endswith(SHEET_SUFFIXES)


def safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_") or "sheet"


def sheets_to_csv(path: Path, out: Path) -> list[Path]:
    """Each sheet of a workbook as a CSV in `out`, named after the sheet (after the file, when it has one sheet)."""
    from .remote import GDAL_ENV
    env = {**GDAL_ENV, **os.environ}
    info = subprocess.run(["ogrinfo", "-ro", "-so", "-json", "-nocount", "-noextent", "-oo", "HEADERS=FORCE", str(path)],
                          capture_output=True, text=True, env=env)
    try:
        sheets = [layer["name"] for layer in json.loads(info.stdout).get("layers", [])]
    except ValueError:
        sheets = []
    if not sheets:
        detail = (info.stderr.strip().splitlines() or ["no sheets"])[-1]
        raise ValueError(f"{path.name} has no sheet GDAL can read ({detail})")
    out.mkdir(parents=True, exist_ok=True)
    made = []
    for sheet in sheets:
        name = safe_name(path.stem if len(sheets) == 1 else sheet)
        target = out / f"{name}.csv"
        # the first row is the header: GDAL only guesses so when its types differ from the rows', and a sheet of text has none
        done = subprocess.run(["ogr2ogr", "-f", "CSV", "-oo", "HEADERS=FORCE", str(target), str(path), sheet],
                              capture_output=True, text=True, env=env)
        if done.returncode != 0:
            raise ValueError(f"{path.name}, sheet {sheet}: {(done.stderr.strip().splitlines() or ['GDAL failed'])[-1]}")
        made.append(target)
    return made


def header(path: Path) -> list[str]:
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        first = f.readline()
    delimiter = "\t" if path.suffix.lower() == ".tsv" or first.count("\t") > first.count(",") else \
        ";" if first.count(";") > first.count(",") else ","
    return next(csv.reader([first], delimiter=delimiter), [])


def points(path: Path, spec: dict | None, project_crs: str | None) -> tuple[list[str], str | None, str]:
    """(GDAL open options, the CRS to assign, a note) for a CSV: which columns make its points."""
    spec = spec or {}
    columns = header(path)
    by_name = {c.strip().lower(): c for c in columns}

    def find(given: str | None, names: tuple[str, ...]) -> str | None:
        if given:
            if given.lower() not in by_name:
                raise ValueError(f"{path.name} has no column {given!r} (it has: {', '.join(columns)})")
            return by_name[given.lower()]
        return next((by_name[n] for n in names if n in by_name), None)

    base = ["KEEP_GEOM_COLUMNS=NO", "AUTODETECT_TYPE=YES"]
    wkt = find(spec.get("wkt"), WKT_NAMES if not (spec.get("x") or spec.get("y")) else ())
    if wkt and not (spec.get("x") or spec.get("y")):
        return [f"GEOM_POSSIBLE_NAMES={wkt}", *base], spec.get("crs") or "EPSG:4326", f"geometry from column {wkt}"
    x, y = find(spec.get("x"), X_NAMES), find(spec.get("y"), Y_NAMES)
    if x and y:
        crs = spec.get("crs") or ("EPSG:4326" if x.strip().lower() in GEOGRAPHIC else project_crs)
        return [f"X_POSSIBLE_NAMES={x}", f"Y_POSSIBLE_NAMES={y}", *base], crs, f"points from columns {x} and {y}"
    if spec.get("x") or spec.get("y"):
        raise ValueError(f"points needs both x and y for {path.name}")
    return ["AUTODETECT_TYPE=YES"], None, "no coordinate columns: loaded without geometry"
