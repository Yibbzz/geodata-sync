"""Each source type against its real service: a small sync (or, where a product is large, a discover), so a
provider changing its API shows at once. Run daily by .github/workflows/sources.yaml, whose results are the
README's source badges (tests/live_badges.py); not in the pull request CI, where a provider's bad day
shouldn't block a merge. Needs the internet, PostGIS (TEST_DB_*) and GDAL:

    LIVE_SOURCES=1 TEST_DB_HOST=localhost ... pytest tests/test_live_sources.py -v

Products are small and long-lived public ones; a badge that fails means the source or the code changed.
"""

import os
import shutil
import time

import pytest
import requests

from geoserver_dataops.cli import main
from test_sync_integration import env, schemas  # noqa: F401 (fixtures)

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.environ.get("LIVE_SOURCES"), reason="LIVE_SOURCES not set: live sources are checked daily"),
              pytest.mark.skipif(shutil.which("ogr2ogr") is None, reason="GDAL's command-line tools not installed")]

OAKHAM = "[-0.76, 52.65, -0.70, 52.69]"

# badge id -> (Source spec, Dataset spec or None to only discover, discover arguments)
CASES = {
    "ckan": ("{ type: ckan, url: https://data.gov.uk }",
             "{ product: conservation-areas125, resource: 9dd40b81-62f5-4c75-b1cd-d200af101632 }", None),
    "os-downloads": ("{ type: os-downloads }", None, ["OpenRoads", "--no-layers"]),
    "spreadsheet": ("{ type: ckan, url: https://data.gov.uk }", "{ product: car-parks16 }", None),  # a CSV with LAT and LONG
    "url": ("{ type: url }", "{ url: https://service.pdok.nl/rvo/natura2000/atom/downloads/natura2000.gpkg }", None),
    "wfs": ("{ type: wfs, url: https://service.pdok.nl/rvo/natura2000/wfs/v1_0 }", "{ product: 'natura2000:natura2000' }", None),
    "ogc-api-features": ("{ type: ogc-api-features, url: https://demo.pygeoapi.io/stable }", "{ product: lakes }", None),
    "arcgis": ("{ type: arcgis, url: https://services.arcgis.com/P3ePLMYs2RVChkJx/arcgis/rest/services }",
               "{ product: 2020_Census_State_Apportionment/FeatureServer/0 }", None),
    "arcgis-hub": ("{ type: ogc-api-records, url: https://hub.arcgis.com/api/search/v1/collections/dataset }",
                   "{ product: 8da5c3ebcc014486a61709ca8e6bfdbb }", None),
    "csw": ("{ type: csw, url: https://www.geocat.ch/geonetwork/srv/eng/csw }",
            "{ product: 6e2b7a2b-4a67-48ad-9829-2d789454250d }", None),
    "atom": ("{ type: atom, url: https://service.pdok.nl/rvo/natura2000/atom/index.xml }", "{ product: natura_2000 }", None),
    "data-europa": ("{ type: data-europa }", "{ product: pdcn_isos_p-canton-geneve }", None),
    # GeoNode's public demo (most of its datasets' exports are broken there; this one works): a failure may be the demo's
    "geonode": ("{ type: geonode, url: https://stable.demo.geonode.org }", "{ product: 'geonode:cfp_401_anillos' }", None),
    "socrata": ("{ type: socrata, url: https://data.cityofnewyork.us }", "{ product: 4j29-i5ry }", None),
    "opendatasoft": ("{ type: opendatasoft, url: https://opendata.paris.fr }", "{ product: marches-decouverts }", None),
    "stac-earth-search": ("{ type: stac, url: https://earth-search.aws.element84.com/v1 }",
                          "{ product: cop-dem-glo-90, stac: { bbox: [-0.8, 52.5, -0.4, 52.8], asset: data }, "
                          "raster: { path: rasters/dem.tif } }", None),
    "stac-planetary-computer": ("{ type: stac, url: https://planetarycomputer.microsoft.com/api/stac/v1 }",
                                f"{{ product: esa-worldcover, stac: {{ bbox: {OAKHAM}, asset: map, datetime: '2021-01-01/2021-12-31' }}, "
                                "raster: { path: rasters/worldcover.tif } }", None),
    "usgs": ("{ type: usgs }", "{ product: 'National Elevation Dataset (NED) 1/3 arc-second', "
             "usgs: { bbox: [-105.30, 40.00, -105.25, 40.05], format: GeoTIFF }, raster: { path: rasters/usgs.tif } }", None),
    "usgs-vector": ("{ type: usgs }", "{ product: 'National Structures Dataset (NSD)', "
                    "usgs: { bbox: [-77.05, 38.88, -77.00, 38.92], format: GeoPackage }, tables: { Struct_Point: {} } }", None),
    "overture": ("{ type: overture }", f"{{ product: places/place, overture: {{ bbox: {OAKHAM} }} }}", None),
    "geofabrik": ("{ type: geofabrik }", "{ product: rutland, tables: { gis_osm_places_free_1: {} } }", None),
    "overpass": ("{ type: overpass }", f"{{ product: 'nwr[amenity=pub]', overpass: {{ bbox: {OAKHAM} }} }}", None),
    "files-s3": ("{ type: files, url: 's3://elevation-tiles-prod/geotiff/9/255/' }",
                 "{ product: 170.tif, raster: { path: rasters/tile.tif } }", None),
    "files-gcs": ("{ type: files, url: 'gs://gcp-public-data-landsat/LC08/01/044/034/LC08_L1GT_044034_20130330_20170310_01_T2/' }",
                  None, ["--limit", "3"]),
    "files-azure": ("{ type: files, url: 'az://ai4edataeuwest/esa-worldcover/v200/2021/map/', credentials: { secret: azure } }",
                    None, ["--search", "N51W003"]),
}


def project(tmp_path, schemas, source: str, dataset: str | None):
    live = schemas
    text = f"""\
apiVersion: dataops/v1alpha1
kind: Project
metadata: {{ name: live }}
spec:
  database:
    connection: {{ secret: testdb }}
    schemas: {{ live: {live[0]}, staging: {live[1]}, backup: {live[2]} }}
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: {{ name: src }}
spec: {source}
"""
    if dataset:
        text += f"---\napiVersion: dataops/v1alpha1\nkind: Dataset\nmetadata: {{ name: sites }}\nspec: {{ source: src, {dataset.strip()[1:]}\n"
    root = tmp_path / "project"
    root.mkdir()
    (root / "project.yaml").write_text(text)
    return root


def run(capsys, *args) -> tuple[int, str]:
    code = main(list(args))
    out = capsys.readouterr()
    return code, out.out + out.err


@pytest.mark.parametrize("case", sorted(CASES))
def test_live_source(case, tmp_path, schemas, env, capsys, monkeypatch):
    source, dataset, discover = CASES[case]
    monkeypatch.setenv("DATAOPS_DATA_DIR", str(tmp_path))
    if case == "files-azure":  # a SAS token for the container, as the Planetary Computer gives one to anyone
        token = requests.get("https://planetarycomputer.microsoft.com/api/sas/v1/token/esa-worldcover", timeout=60).json()["token"]
        monkeypatch.setenv("DATAOPS_AZURE_SASTOKEN", token)
    root = project(tmp_path, schemas, source, dataset)
    if dataset is None:
        code, out = run(capsys, "discover", "src", "-C", str(root), *discover)
        # a listing (PRODUCT), a product's layers (LAYER), or its downloads with the one sync would use
        assert code == 0 and any(t in out for t in ("PRODUCT", "LAYER", "the download sync uses")), out
        return
    for attempt in range(3):  # a busy public server (Overpass at capacity) is asked again, not counted as a failure
        code, out = run(capsys, "sync", "-C", str(root), "sites", "--force")
        if code == 0 or "is busy" not in out:
            break
        time.sleep(60 * (attempt + 1))
    assert code == 0 and "sites: loaded" in out, out
