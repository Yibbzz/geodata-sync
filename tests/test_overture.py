"""The overture source type, against a fake Overture bucket."""

import builtins
import json

import pytest

pytest.importorskip("pyarrow")
pytest.importorskip("shapely")

from geoserver_dataops.sources import NotFound, SourceError  # noqa: E402
from geoserver_dataops.sources.overture import Overture  # noqa: E402
from fake_overture import OvertureBucket  # noqa: E402

AREA = {"bbox": [0, 51, 1, 52]}


@pytest.fixture
def bucket(tmp_path):
    b = OvertureBucket(tmp_path)
    yield b
    b.close()


def test_the_newest_releases_types_are_the_products(bucket):
    listing = Overture(url=bucket.url).products()
    assert [(p.id, p.version) for p in listing.products] == [
        ("buildings/building", "2026-10-21.0"), ("places/place", "2026-10-21.0")]
    assert Overture(url=bucket.url).products("places").total == 1


def test_a_product_must_be_a_theme_and_type(bucket):
    with pytest.raises(NotFound):
        Overture(url=bucket.url).product("places/restaurant")
    with pytest.raises(SourceError, match="isn't an Overture theme and type"):
        Overture(url=bucket.url).product("Places")


def test_the_area_is_part_of_the_version(bucket):
    client = Overture(url=bucket.url)
    one, same, other = (client.selection("places/place", o) for o in (AREA, AREA, {"bbox": [0, 51, 2, 52]}))
    assert one.version.startswith("2026-10-21.0 (") and one.version == same.version != other.version
    with pytest.raises(SourceError, match="is empty"):
        client.selection("places/place", {"bbox": [1, 51, 0, 52]})


def test_only_the_rows_in_the_area_are_written_and_far_row_groups_not_read(bucket, tmp_path, monkeypatch):
    import pyarrow.parquet as pq
    read, real = [], pq.ParquetFile.read_row_group
    monkeypatch.setattr(pq.ParquetFile, "read_row_group", lambda self, i, **kw: read.append(i) or real(self, i, **kw))
    client = Overture(url=bucket.url)
    p = client.selection("places/place", AREA)
    out = tmp_path / "place.geojsonl"
    client.fetch(p.chosen, out)
    features = [json.loads(line) for line in out.read_text().splitlines()]
    assert sorted(f["properties"]["id"] for f in features) == ["a", "b", "c"]  # not d (same file), nor far
    a = next(f for f in features if f["properties"]["id"] == "a")
    assert a["geometry"] == {"type": "Point", "coordinates": [0.1, 51.1]}
    assert a["properties"]["names"] == {"primary": "place a"} and a["properties"]["categories"] == ["cafe"]
    assert "bbox" not in a["properties"]
    assert sorted(read) == [0, 1]  # part-0's two row groups; part-1's one (far away) is never read, only its footer


def test_nothing_in_the_area_says_so(bucket, tmp_path):
    client = Overture(url=bucket.url)
    with pytest.raises(SourceError, match="nothing of places/place is inside"):
        client.fetch(client.selection("places/place", {"bbox": [-50, -50, -40, -40]}).chosen, tmp_path / "x.geojsonl")


def test_without_pyarrow_it_says_what_to_install(bucket, tmp_path, monkeypatch):
    real = builtins.__import__
    monkeypatch.setattr(builtins, "__import__",
                        lambda name, *a, **k: (_ for _ in ()).throw(ImportError(name)) if name.startswith("pyarrow") else real(name, *a, **k))
    client = Overture(url=bucket.url)
    with pytest.raises(SourceError, match=r"pip install 'geoserver-dataops\[overture\]'"):
        client.fetch(client.selection("places/place", AREA).chosen, tmp_path / "x.geojsonl")
