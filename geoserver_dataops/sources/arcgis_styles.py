"""An ArcGIS layer's own symbology (its `drawingInfo`) as an SLD 1.0 style.

ArcGIS describes how a layer is drawn as JSON: a renderer (`simple`: one symbol; `uniqueValue`: a symbol per value
of one to three fields; `classBreaks`: a symbol per range of a number), symbols (`esriSFS` fills, `esriSLS` lines,
`esriSMS` markers, `esriPMS` pictures), the layer's scale range and transparency, and labels. This writes the same
as SLD, for GeoServer to draw the table dataops loaded from the layer. Field names are written as PostgreSQL has
them after the load (GDAL lowercases them). What SLD can't say, or this doesn't convert, is drawn as plainly as it
can be and listed in the style's notes.
"""

from __future__ import annotations

import base64
import re
from xml.sax.saxutils import escape

from ..styles import column

ICONS = "__ICONS__"  # replaced, when the style is written, by the folder its pictures go in (styles.py)
PT = 4 / 3  # ArcGIS sizes are in points; SLD's in pixels

DASHES = {  # esriSLS styles as dash patterns, in multiples of the line's width
    "esriSLSDash": [4, 3], "esriSLSDot": [1, 3], "esriSLSDashDot": [4, 3, 1, 3], "esriSLSDashDotDot": [4, 3, 1, 3, 1, 3],
    "esriSLSShortDash": [3, 2], "esriSLSShortDot": [1, 2], "esriSLSLongDash": [8, 3], "esriSLSShortDashDot": [3, 2, 1, 2],
}
MARKS = {"esriSMSCircle": "circle", "esriSMSSquare": "square", "esriSMSCross": "cross", "esriSMSX": "x",
         "esriSMSDiamond": "square", "esriSMSTriangle": "triangle"}
HATCHES = {"esriSFSHorizontal": "shape://horline", "esriSFSVertical": "shape://vertline",
           "esriSFSForwardDiagonal": "shape://slash", "esriSFSBackwardDiagonal": "shape://backslash",
           "esriSFSCross": "shape://plus", "esriSFSDiagonalCross": "shape://times"}


def colour(c) -> tuple[str, float] | None:
    """[r, g, b, a] (a 0-255) as (#rrggbb, opacity); None for no colour."""
    if not c or len(c) < 3:
        return None
    alpha = (c[3] if len(c) > 3 else 255) / 255
    return "#{:02x}{:02x}{:02x}".format(*[int(v) for v in c[:3]]), round(alpha, 3)


def num(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


class Converter:
    def __init__(self, opacity: float = 1.0):
        self.opacity = opacity  # from the layer's transparency
        self.files: dict[str, bytes] = {}  # pictures: name -> bytes
        self.notes: list[str] = []

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def param(self, name: str, value) -> str:
        return f'<CssParameter name="{name}">{escape(str(value))}</CssParameter>'

    def stroke(self, line: dict | None) -> str:
        if not line or line.get("style") == "esriSLSNull":
            return ""
        c = colour(line.get("color"))
        if c is None:
            return ""
        width = (line.get("width") or 0.75) * PT
        parts = [self.param("stroke", c[0]), self.param("stroke-width", num(width))]
        if c[1] * self.opacity < 1:
            parts.append(self.param("stroke-opacity", num(c[1] * self.opacity)))
        dash = DASHES.get(line.get("style", "esriSLSSolid"))
        if dash:
            parts.append(self.param("stroke-dasharray", " ".join(num(d * max(width, 1)) for d in dash)))
        return f"<Stroke>{''.join(parts)}</Stroke>"

    def fill(self, c) -> str:
        c = colour(c)
        if c is None:
            return ""
        out = self.param("fill", c[0])
        if c[1] * self.opacity < 1:
            out += self.param("fill-opacity", num(c[1] * self.opacity))
        return f"<Fill>{out}</Fill>"

    def picture(self, s: dict) -> str | None:
        """An esriPMS (or esriPFS) picture saved beside the style; its address relative to the styles folder."""
        data, kind = s.get("imageData"), (s.get("contentType") or "image/png")
        if not data:
            return None
        ext = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/svg+xml": "svg"}.get(kind, "png")
        name = f"{len(self.files) + 1}.{ext}"
        try:
            self.files[name] = base64.b64decode(data)
        except ValueError:
            return None
        return f"{ICONS}/{name}"

    def graphic(self, href: str, kind: str, size: float) -> str:
        return (f'<Graphic><ExternalGraphic><OnlineResource xlink:type="simple" xlink:href="{href}"/>'
                f"<Format>{kind}</Format></ExternalGraphic><Size>{num(size)}</Size></Graphic>")

    def symbolizer(self, s: dict | None, geometry: str) -> str:
        """One ArcGIS symbol as an SLD symbolizer."""
        if not s:
            return ""
        t = s.get("type")
        if t == "esriSFS":
            if geometry != "polygon":
                return ""
            style = s.get("style", "esriSFSSolid")
            if style == "esriSFSNull":
                fill = ""
            elif style in HATCHES:
                c = colour(s.get("color")) or ("#000000", 1)
                fill = (f'<Fill><GraphicFill><Graphic><Mark><WellKnownName>{HATCHES[style]}</WellKnownName>'
                        f'<Stroke>{self.param("stroke", c[0])}</Stroke></Mark><Size>8</Size></Graphic></GraphicFill></Fill>')
            else:
                fill = self.fill(s.get("color"))
            return f"<PolygonSymbolizer>{fill}{self.stroke(s.get('outline'))}</PolygonSymbolizer>"
        if t == "esriPFS":
            href = self.picture(s)
            if href is None or geometry != "polygon":
                return ""
            graphic = self.graphic(href, s.get("contentType") or "image/png", (s.get("height") or 16) * PT)
            return f"<PolygonSymbolizer><Fill><GraphicFill>{graphic}</GraphicFill></Fill>{self.stroke(s.get('outline'))}</PolygonSymbolizer>"
        if t == "esriSLS":
            stroke = self.stroke(s)
            return f"<LineSymbolizer>{stroke}</LineSymbolizer>" if stroke else ""
        if t == "esriSMS":
            mark = MARKS.get(s.get("style", "esriSMSCircle"), "circle")
            if s.get("style") == "esriSMSDiamond":
                self.note("diamond markers are drawn as squares turned 45°")
            rotation = 45 + (s.get("angle") or 0) if s.get("style") == "esriSMSDiamond" else (s.get("angle") or 0)
            body = f"<Mark><WellKnownName>{mark}</WellKnownName>{self.fill(s.get('color'))}{self.stroke(s.get('outline'))}</Mark>"
            size = (s.get("size") or 8) * PT
            sym = f"<Graphic>{body}<Size>{num(size)}</Size>" + (f"<Rotation>{num(rotation)}</Rotation>" if rotation else "") + "</Graphic>"
            return f"<PointSymbolizer>{sym}</PointSymbolizer>"
        if t == "esriPMS":
            href = self.picture(s)
            if href is None:
                return ""
            return f"<PointSymbolizer>{self.graphic(href, s.get('contentType') or 'image/png', (s.get('height') or s.get('width') or 16) * PT)}</PointSymbolizer>"
        if t in ("CIMSymbolReference", "esriTS"):
            self.note(f"{t} symbols aren't converted: those features draw plainly")
        else:
            self.note(f"{t} symbols aren't converted")
        return ""

    def plain(self, geometry: str) -> str:
        return {"polygon": '<PolygonSymbolizer><Fill><CssParameter name="fill">#cccccc</CssParameter></Fill>'
                           '<Stroke><CssParameter name="stroke">#666666</CssParameter></Stroke></PolygonSymbolizer>',
                "line": '<LineSymbolizer><Stroke><CssParameter name="stroke">#666666</CssParameter></Stroke></LineSymbolizer>',
                }.get(geometry, '<PointSymbolizer><Graphic><Mark><WellKnownName>circle</WellKnownName><Fill>'
                                '<CssParameter name="fill">#666666</CssParameter></Fill></Mark><Size>6</Size></Graphic></PointSymbolizer>')


def prop(field: str) -> str:
    return f"<ogc:PropertyName>{escape(column(field))}</ogc:PropertyName>"


def literal(v) -> str:
    return f"<ogc:Literal>{escape(str(v))}</ogc:Literal>"


def rule(name: str, title: str, symbolizer: str, filter_: str = "", scales: str = "", else_: bool = False) -> str:
    return (f"<Rule><Name>{escape(name)}</Name><Title>{escape(title)}</Title>"
            + (f"<ogc:Filter>{filter_}</ogc:Filter>" if filter_ else "") + ("<ElseFilter/>" if else_ else "")
            + scales + symbolizer + "</Rule>")


def scale_range(min_scale, max_scale) -> str:
    """ArcGIS's minScale is the most zoomed-out scale shown (0: no limit): SLD's MaxScaleDenominator."""
    out = ""
    if max_scale:
        out += f"<MinScaleDenominator>{num(max_scale)}</MinScaleDenominator>"
    if min_scale:
        out += f"<MaxScaleDenominator>{num(min_scale)}</MaxScaleDenominator>"
    return out


def rules(renderer: dict, geometry: str, cv: Converter, scales: str) -> list[str]:
    t = renderer.get("type")
    if renderer.get("visualVariables"):
        cv.note("visual variables (colour or size by a value) aren't converted: the base symbols are used")
    if t == "simple":
        sym = cv.symbolizer(renderer.get("symbol"), geometry) or cv.plain(geometry)
        return [rule("all", renderer.get("label") or "All features", sym, scales=scales)]
    if t == "uniqueValue":
        fields = [f for f in (renderer.get("field1"), renderer.get("field2"), renderer.get("field3")) if f]
        if not fields:
            cv.note("a unique-value renderer by an Arcade expression isn't converted: every feature draws plainly")
            return [rule("all", "All features", cv.plain(geometry), scales=scales)]
        out = []
        delimiter = renderer.get("fieldDelimiter") or ","
        for i, info in enumerate(renderer.get("uniqueValueInfos") or [], 1):
            values = str(info.get("value", "")).split(delimiter) if len(fields) > 1 else [info.get("value", "")]
            tests = [f"<ogc:PropertyIsEqualTo>{prop(f)}{literal(v.strip() if isinstance(v, str) else v)}</ogc:PropertyIsEqualTo>"
                     for f, v in zip(fields, values)]
            test = tests[0] if len(tests) == 1 else f"<ogc:And>{''.join(tests)}</ogc:And>"
            sym = cv.symbolizer(info.get("symbol"), geometry) or cv.plain(geometry)
            out.append(rule(f"value-{i}", info.get("label") or str(info.get("value")), sym, test, scales))
        if renderer.get("defaultSymbol"):
            sym = cv.symbolizer(renderer["defaultSymbol"], geometry)
            if sym:
                out.append(rule("other", renderer.get("defaultLabel") or "Other", sym, scales=scales, else_=True))
        return out
    if t == "classBreaks":
        field = renderer.get("field")
        if not field:
            cv.note("a class-breaks renderer by an Arcade expression isn't converted: every feature draws plainly")
            return [rule("all", "All features", cv.plain(geometry), scales=scales)]
        if renderer.get("normalizationType") not in (None, "esriNormalizeNone"):
            cv.note(f"class breaks are normalised ({renderer['normalizationType']}) in ArcGIS; here they compare the field itself")
        out, low = [], renderer.get("minValue")
        for i, info in enumerate(renderer.get("classBreakInfos") or [], 1):
            high = info.get("classMaxValue")
            low = info.get("classMinValue", low)
            tests = []
            if low is not None:
                op = "PropertyIsGreaterThanOrEqualTo" if i == 1 else "PropertyIsGreaterThan"
                tests.append(f"<ogc:{op}>{prop(field)}{literal(low)}</ogc:{op}>")
            if high is not None:
                tests.append(f"<ogc:PropertyIsLessThanOrEqualTo>{prop(field)}{literal(high)}</ogc:PropertyIsLessThanOrEqualTo>")
            test = tests[0] if len(tests) == 1 else f"<ogc:And>{''.join(tests)}</ogc:And>"
            sym = cv.symbolizer(info.get("symbol"), geometry) or cv.plain(geometry)
            out.append(rule(f"class-{i}", info.get("label") or f"{low} to {high}", sym, test, scales))
            low = high
        return out
    cv.note(f"the {t} renderer isn't converted: every feature draws plainly")
    return [rule("all", "All features", cv.plain(geometry), scales=scales)]


def label_rules(labeling: list[dict] | None, cv: Converter) -> list[str]:
    """Labels whose text is one field (`[NAME]`, `$feature.NAME`); others are left out, with a note."""
    out = []
    for i, info in enumerate(labeling or [], 1):
        expression = info.get("labelExpression") or ""
        arcade = (info.get("labelExpressionInfo") or {}).get("expression") or ""
        m = re.fullmatch(r"\s*\[([^\]]+)\]\s*", expression) or re.fullmatch(r"\s*\$feature\.(\w+)\s*", arcade) \
            or re.fullmatch(r"""\s*\$feature\[["'](\w+)["']\]\s*""", arcade)
        if not m:
            cv.note(f"a label made from an expression ({(expression or arcade)[:60]}) isn't converted")
            continue
        if info.get("where"):
            cv.note("labels shown only where a condition holds are shown everywhere")
        sym = info.get("symbol") or {}
        font = sym.get("font") or {}
        fill = colour(sym.get("color")) or ("#000000", 1)
        halo = ""
        if sym.get("haloSize") and sym.get("haloColor"):
            hc = colour(sym["haloColor"])
            halo = f"<Halo><Radius>{num(sym['haloSize'] * PT / 2)}</Radius><Fill>{cv.param('fill', hc[0])}</Fill></Halo>"
        style = "italic" if font.get("style") == "italic" else "normal"
        weight = "bold" if font.get("weight") == "bold" else "normal"
        text = (f"<TextSymbolizer><Label>{prop(m.group(1))}</Label><Font>{cv.param('font-family', font.get('family') or 'Noto Sans')}"
                f"{cv.param('font-size', num((font.get('size') or 9) * PT))}{cv.param('font-style', style)}{cv.param('font-weight', weight)}"
                f"</Font>{halo}<Fill>{cv.param('fill', fill[0])}</Fill></TextSymbolizer>")
        out.append(rule(f"label-{i}", info.get("name") or "Labels", text, scales=scale_range(info.get("minScale"), info.get("maxScale"))))
    return out


GEOMETRIES = {"esriGeometryPolygon": "polygon", "esriGeometryPolyline": "line", "esriGeometryPoint": "point",
              "esriGeometryMultipoint": "point"}


def to_sld(info: dict, name: str) -> tuple[str, dict[str, bytes], list[str]] | None:
    """The layer's drawing info as SLD 1.0: (SLD, pictures it points to, notes), or None when the layer has none."""
    drawing = info.get("drawingInfo") or {}
    renderer = drawing.get("renderer")
    geometry = GEOMETRIES.get(info.get("geometryType") or "")
    if not renderer or not geometry:
        return None
    cv = Converter(opacity=1 - (drawing.get("transparency") or 0) / 100)
    scales = scale_range(info.get("minScale"), info.get("maxScale"))
    body = "".join(rules(renderer, geometry, cv, scales))
    labels = "".join(label_rules(drawing.get("labelingInfo"), cv))
    if labels:
        body += f"</FeatureTypeStyle><FeatureTypeStyle>{labels}"
    sld = ('<?xml version="1.0" encoding="UTF-8"?>\n'
           '<StyledLayerDescriptor version="1.0.0" xmlns="http://www.opengis.net/sld" xmlns:ogc="http://www.opengis.net/ogc" '
           'xmlns:xlink="http://www.w3.org/1999/xlink">\n'
           f"<NamedLayer><Name>{escape(name)}</Name><UserStyle><Name>{escape(name)}</Name>"
           f"<Title>{escape(info.get('name') or name)}</Title><FeatureTypeStyle>{body}</FeatureTypeStyle></UserStyle></NamedLayer>\n"
           "</StyledLayerDescriptor>\n")
    return pretty(sld), cv.files, cv.notes


def pretty(sld: str) -> str:
    """Indented, so the file reads well and diffs well once it's in a project."""
    import xml.dom.minidom

    text = xml.dom.minidom.parseString(sld.encode("utf-8")).toprettyxml(indent="  ", encoding="UTF-8").decode("utf-8")
    return "\n".join(line for line in text.splitlines() if line.strip()) + "\n"
