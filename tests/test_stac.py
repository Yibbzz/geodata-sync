"""The stac source type: STAC APIs, against a fake one."""

import shutil

import pytest

from geoserver_dataops.cli import main
from geoserver_dataops.loader import load
from geoserver_dataops.sources import NotFound, SourceError
from geoserver_dataops.sources.stac import MAX_ITEMS, STAC, md5_of
from geoserver_dataops.validate import validate
from fake_stac import STACServer

pytestmark = pytest.mark.skipif(shutil.which("gdal_translate") is None, reason="GDAL's command-line tools not installed")
EVERYTHING = {"bbox": [-1, 49, 5, 52], "asset": "data"}


@pytest.fixture(params=[False, True], ids=["get-paging", "post-paging"])
def server(request, tmp_path):
    s = STACServer(tmp_path, post_paging=request.param)
    yield s
    s.close()


def test_collections_are_the_products(server):
    listing = STAC(url=server.url).products("dem")
    assert [(p.id, p.name) for p in listing.products] == [("dem", "Copernicus DEM")]


def test_a_collection_lists_its_items_files(server):
    p = STAC(url=f"{server.url}/collections/dem").product("dem")  # a collection's address works as the source's
    assert [(d.id, d.loadable) for d in p.downloads] == [("data", True), ("metadata", False)]
    with pytest.raises(NotFound):
        STAC(url=server.url).product("nope")


def test_a_selection_follows_every_page_and_takes_the_asset(server):
    p = STAC(url=server.url).selection("dem", EVERYTHING)
    assert [d.id for d in p.parts] == ["dem-0", "dem-1", "dem-2"]  # two pages of two
    assert all(d.url.endswith(f"/files/tile{i}.tif") for i, d in enumerate(p.parts))
    assert p.version.startswith("3 items, ")
    assert p.parts[0].file_name == "0000-tile0.tif"  # unique: every item's file may have the same name


def test_the_version_changes_only_with_the_items(server):
    client = STAC(url=server.url)
    first = client.selection("dem", EVERYTHING).version
    assert client.selection("dem", EVERYTHING).version == first
    server.items[1]["updated"] = "2026-06-01T00:00:00Z"
    assert client.selection("dem", EVERYTHING).version != first
    assert client.selection("dem", {**EVERYTHING, "bbox": [-1, 49, 0.5, 52]}).version.startswith("1 items, ")


@pytest.mark.parametrize("options, message", [
    ({"bbox": [50, 0, 51, 1], "asset": "data"}, "no items of dem"),
    ({**EVERYTHING, "asset": "visual"}, r"has no asset 'visual' \(it has: data, metadata\)"),
])
def test_what_cant_be_selected_says_why(server, options, message):
    with pytest.raises(SourceError, match=message):
        STAC(url=server.url).selection("dem", options)


def test_too_many_items_asks_for_a_smaller_area(server, monkeypatch):
    import geoserver_dataops.sources.stac as stac
    monkeypatch.setattr(stac, "MAX_ITEMS", 2)
    with pytest.raises(SourceError, match="more than 2 items"):
        STAC(url=server.url).selection("dem", EVERYTHING)
    assert MAX_ITEMS == 500


def test_only_http_files_are_downloaded(server):
    real = server.item
    server.item = lambda i: {**real(i), "assets": {"data": {"href": "file:///etc/passwd", "type": "image/tiff"}}}
    with pytest.raises(SourceError, match="not an http"):
        STAC(url=server.url).selection("dem", EVERYTHING)


def test_md5_comes_out_of_a_multihash():
    assert md5_of("d510" + "ab" * 16) == "ab" * 16
    assert md5_of("1220" + "ab" * 32) is None  # SHA-256: not checked
    assert md5_of(None) is None


def test_discover_lists_a_collections_assets(server, capsys, tmp_path):
    assert main(["discover", "stac", "dem", "--url", server.url, "-C", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "data: image/tiff; application=geotiff; profile=cloud-optimized, Heights" in out
    assert "metadata: application/xml (not a raster)" in out and "`stac: { bbox:" in out


def test_validate_asks_a_stac_dataset_for_its_selection_and_raster(tmp_path):
    (tmp_path / "p.yaml").write_text("""\
apiVersion: dataops/v1alpha1
kind: Project
metadata: { name: t }
spec: { database: { connection: { secret: pg } } }
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: { name: es }
spec: { type: stac, url: "https://earth-search.example/v1" }
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: { name: dem }
spec: { source: es, product: cop-dem-glo-30 }
---
apiVersion: dataops/v1alpha1
kind: Source
metadata: { name: web }
spec: { type: url }
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: { name: other }
spec: { source: web, url: "https://x.example/a.zip", stac: { bbox: [0, 0, 1], asset: data } }
""")
    messages = sorted(p.message for p in validate(load(tmp_path)) if p.severity == "error")
    assert "spec.raster: required for a stac source: the raster file to make" in messages
    assert "spec.stac: required for a stac source: which items and file to load (bbox, asset)" in messages
    assert "spec.stac: only used with a stac source, not url" in messages
    assert any(m.startswith("spec.stac.bbox:") for m in messages)  # three numbers, not four
