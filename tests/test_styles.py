"""Styles that come with the data (styles.py): a GeoPackage's layer_styles, as QGIS saves them, and an SLD beside a
shapefile in a zip. `discover` lists them, `dataops styles` writes them as files, and sync never loads the style
table as data. Real GDAL on small local files, read through a `files` Source."""

import csv
import json
import shutil
import subprocess
import zipfile

import pytest

from geoserver_dataops import ingest, remote
from geoserver_dataops.cli import main
from geoserver_dataops.styles import Style, file_names, sidecars

needs_gdal = pytest.mark.skipif(shutil.which("ogr2ogr") is None, reason="GDAL not installed")

SLD = """<?xml version="1.0" encoding="UTF-8"?>
<StyledLayerDescriptor xmlns="http://www.opengis.net/sld" xmlns:se="http://www.opengis.net/se" version="1.1.0">
  <NamedLayer><se:Name>{name}</se:Name><UserStyle><se:Name>{name}</se:Name><se:FeatureTypeStyle><se:Rule>
    <se:PolygonSymbolizer><se:Fill><se:SvgParameter name="fill">{colour}</se:SvgParameter></se:Fill></se:PolygonSymbolizer>
  </se:Rule></se:FeatureTypeStyle></UserStyle></NamedLayer>
</StyledLayerDescriptor>"""
SQUARE = {"type": "FeatureCollection", "features": [{"type": "Feature", "properties": {"kind": "a"},
          "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [1, 0], [0, 0]]]}}]}


def styled_geopackage(path, rows):
    """A GeoPackage with layers `areas` and `sites`, and QGIS's layer_styles table holding `rows`:
    (layer, style name, default, SLD or "", QML or "")."""
    src = path.parent / "square.geojson"
    src.write_text(json.dumps(SQUARE))
    for layer in ("areas", "sites"):
        subprocess.run(["ogr2ogr", "-f", "GPKG", "-update", "-append", str(path), str(src), "-nln", layer],
                       check=True, capture_output=True)
    table = path.parent / "layer_styles.csv"
    with open(table, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["f_table_catalog", "f_table_schema", "f_table_name", "f_geometry_column", "styleName",
                    "styleQML", "styleSLD", "useAsDefault", "description", "owner", "ui", "update_time"])
        for layer, name, default, sld, qml in rows:
            w.writerow(["", "", layer, "geom", name, qml, sld, "1" if default else "0", "", "", "", ""])
    subprocess.run(["ogr2ogr", "-f", "GPKG", "-update", str(path), str(table), "-nln", "layer_styles"],
                   check=True, capture_output=True)
    return path


@pytest.fixture
def drop(tmp_path):
    """A folder of downloads: sites.gpkg (styled with QGIS) and roads.zip (a shapefile with roads.sld beside it)."""
    folder = tmp_path / "drop"
    folder.mkdir()
    styled_geopackage(folder / "sites.gpkg", [
        ("areas", "Areas by kind", True, SLD.format(name="areas", colour="#ff0000"), "<qgis/>"),
        ("areas", "Outline only", False, SLD.format(name="areas", colour="#ffffff"), "<qgis/>"),
        ("sites", "Sites", True, "", "<qgis/>"),  # saved without SLD: QML only
    ])
    shp = tmp_path / "shp"
    shp.mkdir()
    (tmp_path / "square.geojson").write_text(json.dumps(SQUARE))
    subprocess.run(["ogr2ogr", str(shp / "roads.shp"), str(tmp_path / "square.geojson"), "-nln", "roads"], check=True, capture_output=True)
    (shp / "roads.sld").write_text(SLD.format(name="roads", colour="#0000ff"))
    with zipfile.ZipFile(folder / "roads.zip", "w") as z:
        for f in sorted(shp.iterdir()):
            z.write(f, f"data/{f.name}")
    return folder


@pytest.fixture
def project(tmp_path, drop):
    root = tmp_path / "project"
    root.mkdir()
    (root / "project.yaml").write_text(f"""\
apiVersion: dataops/v1alpha1
kind: Project
metadata: {{ name: t }}
spec: {{ database: {{ connection: {{ secret: db }} }} }}
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: {{ name: drop }}
spec: {{ type: files, url: "{drop}/" }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: sites }}
spec: {{ source: drop, product: sites.gpkg, tables: {{ areas: {{ name: site_areas }}, sites: {{}} }} }}
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: {{ name: roads }}
spec: {{ source: drop, product: roads.zip }}
""")
    return root


@pytest.fixture(autouse=True)
def no_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@needs_gdal
def test_a_geopackages_styles_go_with_its_layers(drop):
    with open(drop / "sites.gpkg", "rb") as f:
        layers = remote.archive_layers(f, str(drop / "sites.gpkg"))
    assert [x.name for x in layers] == ["areas", "sites"]  # layer_styles isn't a layer
    areas, sites = layers
    assert [(s.name, s.default, s.origin) for s in areas.styles] == [
        ("Areas by kind", True, "the GeoPackage's layer_styles"), ("Outline only", False, "the GeoPackage's layer_styles")]
    assert "#ff0000" in areas.styles[0].sld
    assert [(s.name, s.sld) for s in sites.styles] == [("Sites", None)]  # QML only


@needs_gdal
def test_an_sld_beside_a_shapefile_goes_with_it(drop):
    with open(drop / "roads.zip", "rb") as f:
        layers = remote.archive_layers(f, str(drop / "roads.zip"))
    assert [(x.name, [(s.name, s.default, s.origin) for s in x.styles]) for x in layers] == [
        ("roads", [("roads", True, "roads.sld beside the data")])]
    assert "#0000ff" in layers[0].styles[0].sld


@needs_gdal
def test_sync_never_loads_the_style_table(drop, tmp_path):
    found = ingest.file_layers(drop / "sites.gpkg", tmp_path)
    assert [layer for _, layer, _ in found] == ["areas", "sites"]


@needs_gdal
def test_discover_lists_the_styles(project, capsys):
    assert main(["discover", "-C", str(project), "drop", "sites.gpkg"]) == 0
    out = capsys.readouterr().out
    assert "layer_styles" not in out.split("STYLES IN THE DATA")[0]
    styles = out.split("STYLES IN THE DATA")[1]
    assert '"Areas by kind" (default)  SLD' in styles and '"Outline only"' in styles
    assert '"Sites" (default)' in styles and "QML only" in styles
    assert "dataops styles <dataset> --to <folder>" in out


@needs_gdal
def test_styles_writes_them_named_after_the_tables(project, tmp_path, capsys):
    to = tmp_path / "publish" / "styles"
    assert main(["styles", "-C", str(project), "--to", str(to)]) == 0
    out = capsys.readouterr().out
    assert sorted(p.name for p in to.iterdir()) == ["roads-roads.sld", "site-areas-outline-only.sld", "site-areas.sld"]
    assert "#ff0000" in (to / "site-areas.sld").read_text() and "#ffffff" in (to / "site-areas-outline-only.sld").read_text()
    assert "in the Layer on site_areas: style: site-areas" in out
    assert "in the Layer on site_areas: styles: [site-areas-outline-only]" in out
    assert "style \"Sites\" for sites_sites is in QGIS's own format (QML) only" in out

    # again: nothing to do
    assert main(["styles", "-C", str(project), "--to", str(to), "sites"]) == 0
    assert "is up to date" in capsys.readouterr().out

    # an edited file is kept, unless --force
    (to / "site-areas.sld").write_text("<!-- my own -->")
    assert main(["styles", "-C", str(project), "--to", str(to), "sites"]) == 1
    assert "differs from the style in the data, and was left as it is; --force replaces it" in capsys.readouterr().out
    assert (to / "site-areas.sld").read_text() == "<!-- my own -->"
    assert main(["styles", "-C", str(project), "--to", str(to), "sites", "--force"]) == 0
    assert "#ff0000" in (to / "site-areas.sld").read_text()


def test_an_unknown_dataset(project, tmp_path, capsys):
    assert main(["styles", "-C", str(project), "--to", str(tmp_path / "out"), "nope"]) == 2
    assert "no Dataset named nope" in capsys.readouterr().err


def test_file_names():
    def s(name, default, sld="<sld/>"):
        return Style(name, sld, default, "x")
    named = file_names("conservation_areas", [s("By designation", False), s("Conservation areas", True), s("Q", True, None)])
    assert [(st.name, f) for st, f in named] == [
        ("Conservation areas", "conservation-areas.sld"), ("By designation", "conservation-areas-by-designation.sld")]
    # none marked default: the first is
    assert [f for _, f in file_names("roads", [s("A", False), s("B", False)])] == ["roads.sld", "roads-b.sld"]


def test_sidecars_are_beside_the_file_and_named_after_it():
    entries = ["data/roads.shp", "data/roads.dbf", "data/Roads.SLD", "data/rivers.sld", "styles/roads.sld"]
    assert sidecars(entries, "data/roads.shp", "roads") == ["data/Roads.SLD"]


def test_a_styles_pictures_are_written_beside_it(tmp_path):
    from geoserver_dataops.styles import ICONS, write
    sld = f'<StyledLayerDescriptor><OnlineResource href="{ICONS}/1.png"/></StyledLayerDescriptor>'
    style = Style("Events (ArcGIS symbology)", sld, True, "the ArcGIS layer's drawing info", {"1.png": b"png"}, ["a note"])
    lines, refused = write("events", "events_points", [style], tmp_path, force=False)
    assert refused == 0
    assert (tmp_path / "icons" / "events-points" / "1.png").read_bytes() == b"png"
    assert 'href="icons/events-points/1.png"' in (tmp_path / "events-points.sld").read_text()
    assert lines[1:] == ["  in the Layer on events_points: style: events-points", "  note: a note"]


def test_attributes_are_named_as_the_load_names_the_columns():
    from geoserver_dataops.styles import as_columns
    sld = ("<Filter><ogc:PropertyIsEqualTo><ogc:PropertyName>BoroName</ogc:PropertyName><ogc:Literal>Queens</ogc:Literal>"
           "</ogc:PropertyIsEqualTo></Filter><Label><se:PropertyName> Road-Class </se:PropertyName></Label>"
           "<PropertyName>name</PropertyName>")
    assert as_columns(sld) == (
        "<Filter><ogc:PropertyIsEqualTo><ogc:PropertyName>boroname</ogc:PropertyName><ogc:Literal>Queens</ogc:Literal>"
        "</ogc:PropertyIsEqualTo></Filter><Label><se:PropertyName>road_class</se:PropertyName></Label>"
        "<PropertyName>name</PropertyName>")
