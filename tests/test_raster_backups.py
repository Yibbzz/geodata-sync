"""Raster backups (geodata_sync/raster.py, install): each dataset prunes only its own."""

from geodata_sync import raster
from test_raster import data  # noqa: F401 (fixture)


def test_pruning_keeps_to_the_datasets_own_backups(tmp_path, data):
    """terrain.tif's backups are terrain.<stamp>.tif; terrain.coarse.<stamp>.tif belong to another dataset."""
    backups = data / raster.BACKUPS / "rasters"
    backups.mkdir(parents=True)
    others = [backups / f"terrain.coarse.2026010{i}_000000.tif" for i in range(1, 4)]
    for path in others:
        path.write_bytes(b"x")
    (data / "rasters").mkdir()
    for i in range(3):
        (data / "rasters" / "terrain.tif").write_bytes(b"live")
        new = tmp_path / f"new{i}.tif"
        new.write_bytes(b"new")
        raster.install(new, "rasters/terrain.tif", keep=1)
    assert all(p.exists() for p in others)
    assert len(list(backups.glob("terrain.2*.tif"))) == 1


def test_a_new_file_doesnt_keep_the_old_ones_statistics(tmp_path, data):
    """GDAL caches a file's statistics beside it in .aux.xml; they would be read as the new file's."""
    (data / "dtm.tif").write_bytes(b"old")
    (data / "dtm.tif.aux.xml").write_text("<PAMDataset><Metadata><MDI key='STATISTICS_MAXIMUM'>30</MDI></Metadata></PAMDataset>")
    new = tmp_path / "new.tif"
    new.write_bytes(b"new")
    raster.install(new, "dtm.tif", keep=1)
    assert (data / "dtm.tif").read_bytes() == b"new" and not (data / "dtm.tif.aux.xml").exists()
