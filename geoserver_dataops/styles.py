"""Styles that come with the data, and `dataops styles`, which writes them as files.

A download can say how its layers should look:

- a GeoPackage saved from QGIS keeps its styles in a `layer_styles` table: each row a style for one layer,
  as SLD (and QGIS's own QML), one of them the layer's default;
- a zip can hold an SLD beside a data file, named after it: `roads.sld` beside `roads.shp`.

`discover` lists them with the layers. `dataops styles` writes each Dataset's styles as SLD files in a folder,
named after the tables the layers load into (table `conservation_areas`: `conservation-areas.sld`), where GeoServer
ConfigOps publishes them: a Layer then names the style. The files are written once, by hand, and committed: the
nightly `sync` never writes them, so they can be edited like any other style.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

STYLE_TABLE = "layer_styles"  # QGIS's table of styles in a GeoPackage (and in PostGIS)
MAX_SLD = 5_000_000  # bytes: larger than any real style; a file this big beside the data isn't one

# Source types whose data never comes with a style: services and APIs that give rows or pixels, not files a
# person saved. `styles` says so instead of reading them.
NO_STYLES = {"overture", "overpass", "socrata", "opendatasoft", "stac", "usgs", "wfs", "ogc-api-features", "database"}


@dataclass
class Style:
    name: str  # the publisher's name for it
    sld: str | None  # the SLD; None when the file has only QGIS's own format (QML)
    default: bool  # the layer's default style
    origin: str  # where it was found, for the user: "the GeoPackage's layer_styles", "roads.sld beside the data"

    def to_dict(self) -> dict:
        return {"name": self.name, "sld": self.sld, "default": self.default, "origin": self.origin}

    @classmethod
    def from_dict(cls, d: dict) -> "Style":
        return cls(d.get("name", ""), d.get("sld"), bool(d.get("default")), d.get("origin", ""))


def is_style_table(name: str) -> bool:
    """A layer that holds styles, not data: never loaded or listed as a layer."""
    return name.lower() == STYLE_TABLE


def truthy(value) -> bool:
    """useAsDefault is a boolean in QGIS's table, but GDAL may give it as 1, "1" or "true"."""
    return str(value).strip().lower() in ("1", "true", "t", "yes")


def geopackage_styles(gdal_path: str) -> dict[str, list[Style]]:
    """The styles in a GeoPackage's layer_styles table, by the layer they are for. Read through GDAL, so a
    GeoPackage on a server or inside a zip is read in place, like its layers."""
    from .remote import RemoteError, input_drivers

    sql = f"SELECT f_table_name, styleName, useAsDefault, styleSLD, length(styleQML) AS qml FROM {STYLE_TABLE}"
    args = ["ogrinfo", "-ro", "-json", "-features", *input_drivers(gdal_path), gdal_path, "-sql", sql]
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise RemoteError(f"couldn't read the styles in {STYLE_TABLE}: {result.stderr.strip()[-300:]}")
    found: dict[str, list[Style]] = {}
    for layer in json.loads(result.stdout).get("layers", []):
        for feature in layer.get("features", []):
            p = feature.get("properties") or {}
            table = p.get("f_table_name")
            sld = (p.get("styleSLD") or "").strip() or None
            if not table or (sld is None and not p.get("qml")):
                continue  # an empty row
            found.setdefault(table, []).append(Style(
                name=p.get("styleName") or table, sld=sld, default=truthy(p.get("useAsDefault")),
                origin=f"the GeoPackage's {STYLE_TABLE}"))
    return found


def sidecars(entries: list[str], data_file: str, layer: str) -> list[str]:
    """SLD files beside a data file, named after the file or the layer: roads.sld beside roads.shp."""
    folder, _, file_name = data_file.rpartition("/")
    stems = {Path(file_name).stem.lower(), layer.lower()}
    return sorted(e for e in entries
                  if e.lower().endswith(".sld") and e.rpartition("/")[0] == folder
                  and Path(e.rpartition("/")[2]).stem.lower() in stems)


def attach(layers, gdal_path: str, entries: list[str] | None = None, read=None, data_file: str = "") -> list:
    """`layers` (those of one data file) without the style table, each with the styles meant for it:
    from the GeoPackage's layer_styles (read at `gdal_path`), and SLD files beside the data (`entries`, the
    archive's file list, and `read(entry) -> bytes`)."""
    from .remote import RemoteError

    data = [layer for layer in layers if not is_style_table(layer.name)]
    if len(data) < len(layers):
        try:
            by_layer = geopackage_styles(gdal_path)
        except RemoteError as e:
            print(f"dataops: note: {e}", file=sys.stderr)
            by_layer = {}
        for layer in data:
            layer.styles = by_layer.get(layer.name, [])
    if entries and read and data_file:
        for layer in data:
            for entry in sidecars(entries, data_file, layer.name):
                try:
                    raw = read(entry)
                except Exception as e:  # noqa: BLE001 - one unreadable file shouldn't stop the listing
                    print(f"dataops: note: couldn't read {entry}: {e}", file=sys.stderr)
                    continue
                if len(raw) > MAX_SLD:
                    continue
                layer.styles = [*layer.styles, Style(
                    name=Path(entry).stem, sld=raw.decode("utf-8", errors="replace"),
                    default=not layer.styles, origin=f"{entry.rpartition('/')[2]} beside the data")]
    return data


# --- writing them out


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "style"


def file_names(table: str, styles: list[Style]) -> list[tuple[Style, str]]:
    """Each SLD style and its file name: the default one named after the table (`conservation-areas.sld`), the
    others after the table and their own name (`conservation-areas-by-designation.sld`)."""
    base = slug(table)
    usable = [s for s in styles if s.sld]
    if usable and not any(s.default for s in usable):
        usable[0] = Style(usable[0].name, usable[0].sld, True, usable[0].origin)  # the first, when none is marked
    named, taken = [], set()
    for s in sorted(usable, key=lambda s: not s.default):
        name = base if s.default and base not in taken else f"{base}-{slug(s.name)}"
        while name in taken:
            name += "-2"
        taken.add(name)
        named.append((s, f"{name}.sld"))
    return named


def write(dataset: str, table: str, styles: list[Style], to: Path, force: bool) -> tuple[list[str], int]:
    """Write one table's styles into `to`: lines for the user, and how many files couldn't be written."""
    lines, refused = [], 0
    for style in styles:
        if not style.sld:
            lines.append(f"{dataset}: note: style \"{style.name}\" for {table} is in QGIS's own format (QML) only; "
                         "open the data in QGIS and save the style as SLD")
    for style, file_name in file_names(table, styles):
        path = to / file_name
        text = style.sld if style.sld.endswith("\n") else style.sld + "\n"
        what = f"(table {table}: \"{style.name}\", from {style.origin})"
        if path.exists():
            if path.read_text(encoding="utf-8", errors="replace") == text:
                lines.append(f"{dataset}: {path} is up to date {what}")
                continue
            if not force:
                lines.append(f"{dataset}: {path} differs from the style in the data, and was left as it is; "
                             "--force replaces it")
                refused += 1
                continue
        path.write_text(text, encoding="utf-8")
        lines.append(f"{dataset}: wrote {path} {what}")
        if style.default:
            lines.append(f"  in the Layer on {table}: style: {path.stem}")
        else:
            lines.append(f"  in the Layer on {table}: styles: [{path.stem}]")
    return lines, refused


def run(root: Path, names: list[str], to: Path, force: bool = False) -> int:
    """`dataops styles`: write the styles that come with each Dataset's data into `to`."""
    from . import discover, remote
    from .loader import load
    from .sources import SourceError
    from .sync import SyncError, plan_layers, product_for
    from .validate import validate
    from .ingest import LoadError

    project = load(root)
    errors = [p for p in validate(project) if p.severity == "error"]
    if errors:
        raise SyncError(f"the files have {len(errors)} error(s); `dataops validate` lists them")
    datasets = [r for r in project.resources if r.kind == "Dataset"]
    unknown = sorted(set(names) - {d.name for d in datasets})
    if unknown:
        raise SyncError(f"no Dataset named {', '.join(unknown)}")
    if names:
        datasets = [d for d in datasets if d.name in names]
    to.mkdir(parents=True, exist_ok=True)
    project_crs = next((r.spec.get("crs") for r in project.resources if r.kind == "Project"), None) or "EPSG:4326"

    failed = 0
    for dataset in datasets:
        if dataset.spec.get("raster"):
            print(f"{dataset.name}: a raster: its styles aren't read")
            continue
        try:
            src = discover.resolve(dataset.spec["source"], root)
            if src.type in NO_STYLES:
                print(f"{dataset.name}: {src.type} data comes without styles")
                continue
            product = product_for(src, dataset)
            if product.chosen is None:
                raise LoadError(f"{product.id} has nothing dataops can load")
            if product.chosen.service and not hasattr(src.client, "styles"):
                print(f"{dataset.name}: {product.chosen.format} services give no styles")
                continue
            layers = discover.product_layers(src, product)
            by_name = {layer.name: layer for layer in layers}
            loaded = plan_layers(dataset, [(layer.file, layer.name, layer.crs) for layer in layers], project_crs)
        except (SourceError, remote.RemoteError, LoadError, discover.DiscoverError) as e:
            print(f"{dataset.name}: failed: {e}", file=sys.stderr)
            failed += 1
            continue
        lines, refused = [], 0
        for layer in loaded:
            styles = by_name[layer.layer].styles
            if styles:
                more, n = write(dataset.name, layer.table, styles, to, force)
                lines += more
                refused += n
        print("\n".join(lines) if lines else f"{dataset.name}: no styles in the data")
        failed += 1 if refused else 0
    return 1 if failed else 0
