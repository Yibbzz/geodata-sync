"""Raster Datasets: a downloaded product made into one Cloud-Optimised GeoTIFF on the data volume.

Every raster in the download (inside zips, and zips inside those, as OS packs its
tiles) is mosaicked through a GDAL VRT and written as one deflate-compressed COG
with overviews. It's written beside the live file, checked, then renamed over it,
so GeoServer never reads half a file; the file it replaces goes to
.geodata-backups/ beside the data, and the newest `backups` copies are kept.

The data folder is /data, where GeoServer's file stores read; GEODATA_DATA_DIR points
elsewhere (a laptop, the e2e tests).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from .ingest import LoadError, check_unpacked_size, gdal_errors, stamp

DATA_ENV = "GEODATA_DATA_DIR"
RASTER_SUFFIXES = {".tif", ".tiff", ".asc", ".img", ".jp2", ".dem", ".bil", ".flt"}
BACKUPS = ".geodata-backups"
MAX_ZIP_DEPTH = 4  # zips inside zips inside zips: OS goes two deep


def data_root() -> Path:
    return Path(os.environ.get(DATA_ENV, "/data"))


def live_path(path: str) -> Path:
    return data_root() / path.strip("/")


def refuse_pointers(rasters: list[Path]) -> None:
    """
    Refuse a "raster" that is really instructions to GDAL. GDAL picks a driver from a file's content,
    and the formats that name other files or addresses as their source (VRT, WMS, tile indexes, STAC)
    are XML or JSON: a VRT called tile.tif could have any local file read into the published COG.
    No raster format read here starts with < or {.
    """
    for path in rasters:
        with open(path, "rb") as f:
            start = f.read(64).removeprefix(b"\xef\xbb\xbf").lstrip()
        if start[:1] in (b"<", b"{"):
            raise LoadError(f"{path.name} isn't raster data: it's an XML or JSON file that would point GDAL elsewhere")


def unpack(download: Path, work: Path) -> list[Path]:
    """The raster files in a download: the file itself, or those in its zips (and zips in those)."""
    if download.suffix.lower() in RASTER_SUFFIXES:
        refuse_pointers([download])
        return [download]
    if not zipfile.is_zipfile(download):
        raise LoadError(f"{download.name} is neither a raster nor a zip of rasters")
    out = work / "unpacked"
    pending, unpacked = [(download, 1)], 0
    while pending:
        archive, depth = pending.pop()
        try:
            z = zipfile.ZipFile(archive)
        except zipfile.BadZipFile:
            raise LoadError(f"{archive.name}, inside {download.name}, isn't a zip that can be read") from None
        with z:
            unpacked += sum(i.file_size for i in z.infolist())
            check_unpacked_size(unpacked)
            for name in z.namelist():
                if name.endswith("/") or ".." in Path(name).parts:
                    continue
                target = out / archive.stem / name
                if name.lower().endswith(".zip"):
                    if depth >= MAX_ZIP_DEPTH:
                        raise LoadError(f"{download.name} has zips nested more than {MAX_ZIP_DEPTH} deep")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    pending.append((target, depth + 1))
                elif Path(name).suffix.lower() in RASTER_SUFFIXES | {".prj", ".aux", ".xml", ".tfw", ".hdr"}:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(name) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    rasters = sorted(p for p in out.rglob("*") if p.suffix.lower() in RASTER_SUFFIXES and not p.name.endswith(".aux.xml"))
    if not rasters:
        raise LoadError(f"no rasters in {download.name} ({', '.join(sorted(RASTER_SUFFIXES))})")
    refuse_pointers(rasters)
    return rasters


def gdal(*cmd: str) -> None:
    result = subprocess.run(list(cmd), capture_output=True, text=True)
    if result.returncode != 0:
        raise LoadError(f"{cmd[0]} failed: {gdal_errors(result.stderr)}")


def info(path: Path) -> dict:
    result = subprocess.run(["gdalinfo", "-json", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise LoadError(f"GDAL can't read {path.name}: {gdal_errors(result.stderr)}")
    return json.loads(result.stdout)


def build(rasters: list[Path], work: Path, out: Path, crs: str | None, fallback: str | None, nodata: float | None = None) -> dict:
    """
    Mosaic `rasters` into one COG at `out`. `crs` is assigned if given; `fallback` if the rasters
    have none. `nodata` marks where no raster covers (GDAL would write 0 there) and the rasters' own gaps.
    """
    listing = work / "rasters.txt"
    listing.write_text("".join(f"{p}\n" for p in rasters))
    vrt = work / "mosaic.vrt"
    has_crs = bool(info(rasters[0]).get("coordinateSystem", {}).get("wkt"))
    assign = crs or (None if has_crs else fallback)
    srs = ["-a_srs", assign] if assign else []
    empty = ["-vrtnodata", str(nodata), "-srcnodata", str(nodata)] if nodata is not None else []
    gdal("gdalbuildvrt", "-q", *srs, *empty, "-input_file_list", str(listing), str(vrt))
    gdal("gdal_translate", "-q", "-of", "COG", "-co", "COMPRESS=DEFLATE", "-co", "PREDICTOR=YES", "-co", "BIGTIFF=IF_SAFER",
         "-co", "NUM_THREADS=ALL_CPUS", "-co", "OVERVIEWS=AUTO", str(vrt), str(out))
    built = info(out)
    width, height = built.get("size", [0, 0])
    if not width or not height:
        raise LoadError("the COG came out empty")
    if not built.get("coordinateSystem", {}).get("wkt"):
        raise LoadError("the rasters have no CRS: set the dataset's raster.crs")
    return built


def install(new: Path, path: str, keep: int) -> None:
    """Rename `new` over the live file; the old one goes to the backups, the newest `keep` kept."""
    live = live_path(path)
    if live.exists():
        backups = data_root() / BACKUPS / Path(path).parent
        backups.mkdir(parents=True, exist_ok=True)
        os.replace(live, backups / f"{live.stem}.{stamp()}{live.suffix}")
        # only this file's backups, <stem>.<timestamp><suffix>: terrain.coarse.<timestamp>.tif is another dataset's
        own = re.compile(re.escape(live.stem) + r"\.[0-9][0-9_]*" + re.escape(live.suffix))
        old = sorted(p for p in backups.iterdir() if own.fullmatch(p.name))
        for extra in old[:-keep] if keep else old:
            extra.unlink()
    os.replace(new, live)
    # GDAL keeps a file's statistics and other details beside it (.aux.xml): those of the old file would be read as the new one's
    live.with_name(live.name + ".aux.xml").unlink(missing_ok=True)


def load(downloads: Path | list[Path], work: Path, path: str, crs: str | None, fallback: str | None, keep: int,
         nodata: float | None = None) -> int:
    """Make the download (or downloads: a STAC selection's files) into the COG at `path` on the data volume;
    returns its size in pixels."""
    if not data_root().is_dir():
        raise LoadError(f"no data volume at {data_root()}: mount GeoServer's data folder there (or set {DATA_ENV})")
    live = live_path(path)
    live.parent.mkdir(parents=True, exist_ok=True)
    downloads = downloads if isinstance(downloads, list) else [downloads]
    rasters = [r for i, d in enumerate(downloads) for r in unpack(d, work / f"unpacked-{i}" if len(downloads) > 1 else work)]
    new = live.with_name(f".{live.name}.{datetime.now():%Y%m%d%H%M%S}.new")  # beside the live file: the rename stays on one volume
    try:
        built = build(rasters, work, new, crs, fallback, nodata)
        install(new, path, keep)
    finally:
        new.unlink(missing_ok=True)
    width, height = built["size"]
    return width * height
