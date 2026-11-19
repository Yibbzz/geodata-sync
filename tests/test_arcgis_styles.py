"""An ArcGIS layer's drawing info as SLD (sources/arcgis_styles.py): renderers, symbols, scales, labels, and notes
for what isn't converted. The layer JSON is as ArcGIS Server and ArcGIS Online give it (trimmed)."""

import base64
import xml.etree.ElementTree as ET

from geoserver_dataops.sources.arcgis_styles import ICONS, to_sld

NS = {"sld": "http://www.opengis.net/sld", "ogc": "http://www.opengis.net/ogc", "xlink": "http://www.w3.org/1999/xlink"}
PNG = base64.b64encode(b"\x89PNG fake").decode()


def layer(geometry, renderer, **drawing):
    return {"name": "Things", "geometryType": geometry, "minScale": 0, "maxScale": 0,
            "drawingInfo": {"renderer": renderer, "transparency": 0, **drawing}}


def rules(sld):
    return ET.fromstring(sld.encode()).findall(".//sld:Rule", NS)


def params(el, kind):
    return {p.get("name"): p.text for p in el.iter(f"{{{NS['sld']}}}CssParameter") if p.get("name").startswith(kind)}


def test_a_simple_fill_as_nyc_planning_draws_its_boroughs():
    sld, files, notes = to_sld(layer("esriGeometryPolygon", {"type": "simple", "symbol": {
        "type": "esriSFS", "style": "esriSFSSolid", "color": [186, 241, 245, 255],
        "outline": {"type": "esriSLS", "style": "esriSLSSolid", "color": [110, 110, 110, 255], "width": 0.4}}}), "boroughs")
    (rule,) = rules(sld)
    assert params(rule, "fill") == {"fill": "#baf1f5"}
    assert params(rule, "stroke") == {"stroke": "#6e6e6e", "stroke-width": "0.53"}  # 0.4 pt
    assert files == {} and notes == []


def test_unique_values_with_picture_markers_and_a_default():
    sld, files, notes = to_sld(layer("esriGeometryPoint", {
        "type": "uniqueValue", "field1": "EventType",
        "uniqueValueInfos": [
            {"value": "17", "label": "Telephone", "symbol": {"type": "esriPMS", "imageData": PNG, "contentType": "image/png", "width": 17, "height": 17}},
            {"value": "1", "label": "Aerial Hazard", "symbol": {"type": "esriSMS", "style": "esriSMSSquare", "color": [255, 0, 0, 255], "size": 6}},
        ],
        "defaultSymbol": {"type": "esriSMS", "style": "esriSMSCircle", "color": [0, 103, 176, 255], "size": 4},
        "defaultLabel": "<all other values>"}), "events")
    telephone, aerial, other = rules(sld)
    assert telephone.find("sld:Title", NS).text == "Telephone"
    assert telephone.find(".//ogc:PropertyName", NS).text == "eventtype"  # as PostgreSQL has the column
    assert telephone.find(".//ogc:Literal", NS).text == "17"
    href = telephone.find(".//sld:OnlineResource", NS).get(f"{{{NS['xlink']}}}href")
    assert href == f"{ICONS}/1.png" and files == {"1.png": b"\x89PNG fake"}
    assert aerial.find(".//sld:WellKnownName", NS).text == "square"
    assert other.find("sld:ElseFilter", NS) is not None and other.find("sld:Title", NS).text == "<all other values>"


def test_unique_values_of_two_fields():
    sld, _, _ = to_sld(layer("esriGeometryPolyline", {
        "type": "uniqueValue", "field1": "Class", "field2": "Lanes", "fieldDelimiter": "|",
        "uniqueValueInfos": [{"value": "A|2", "label": "A road, 2 lanes",
                              "symbol": {"type": "esriSLS", "style": "esriSLSDash", "color": [0, 0, 0, 255], "width": 1.5}}]}), "roads")
    (rule,) = rules(sld)
    tests = rule.findall(".//ogc:PropertyIsEqualTo", NS)
    assert [(t.find("ogc:PropertyName", NS).text, t.find("ogc:Literal", NS).text) for t in tests] == [("class", "A"), ("lanes", "2")]
    assert params(rule, "stroke")["stroke-dasharray"] == "8 6"  # a dash 4 widths long, 2 px wide


def test_class_breaks_are_ranges():
    sld, _, _ = to_sld(layer("esriGeometryPolygon", {
        "type": "classBreaks", "field": "POP", "minValue": 0,
        "classBreakInfos": [
            {"classMaxValue": 1000, "label": "Small", "symbol": {"type": "esriSFS", "style": "esriSFSSolid", "color": [255, 255, 0, 255]}},
            {"classMaxValue": 5000, "label": "Large", "symbol": {"type": "esriSFS", "style": "esriSFSSolid", "color": [255, 0, 0, 255]}},
        ]}), "places")
    small, large = rules(sld)
    assert [(e.tag.split("}")[1], e.find("ogc:Literal", NS).text) for e in small.find(".//ogc:And", NS)] == [
        ("PropertyIsGreaterThanOrEqualTo", "0"), ("PropertyIsLessThanOrEqualTo", "1000")]
    assert [(e.tag.split("}")[1], e.find("ogc:Literal", NS).text) for e in large.find(".//ogc:And", NS)] == [
        ("PropertyIsGreaterThan", "1000"), ("PropertyIsLessThanOrEqualTo", "5000")]


def test_transparency_scales_and_hatching():
    info = layer("esriGeometryPolygon", {"type": "simple", "symbol": {
        "type": "esriSFS", "style": "esriSFSForwardDiagonal", "color": [0, 0, 0, 255],
        "outline": {"type": "esriSLS", "style": "esriSLSSolid", "color": [0, 0, 0, 255], "width": 1}}}, transparency=50)
    info["minScale"], info["maxScale"] = 250000, 1000
    (rule,) = rules(to_sld(info, "zones")[0])
    assert rule.find("sld:MinScaleDenominator", NS).text == "1000"     # ArcGIS's maxScale: the closest it's shown
    assert rule.find("sld:MaxScaleDenominator", NS).text == "250000"   # ArcGIS's minScale: the farthest
    assert rule.find(".//sld:WellKnownName", NS).text == "shape://slash"
    assert params(rule, "stroke")["stroke-opacity"] == "0.5"


def test_labels_from_one_field_and_notes_for_the_rest():
    sld, _, notes = to_sld(layer("esriGeometryPoint", {"type": "heatmap"}, labelingInfo=[
        {"labelExpression": "[Description]", "minScale": 50000, "symbol": {
            "type": "esriTS", "color": [0, 0, 0, 255], "haloColor": [255, 255, 255, 255], "haloSize": 2,
            "font": {"family": "Arial", "size": 10, "weight": "bold"}}},
        {"labelExpressionInfo": {"expression": "$feature.NAME + ' (' + $feature.CODE + ')'"}},
    ]), "points")
    plain, label = rules(sld)
    assert plain.find(".//sld:PointSymbolizer", NS) is not None  # a heatmap: drawn plainly
    assert label.find(".//ogc:PropertyName", NS).text == "description"
    assert params(label, "font") == {"font-family": "Arial", "font-size": "13.33", "font-style": "normal", "font-weight": "bold"}
    assert label.find("sld:MaxScaleDenominator", NS).text == "50000"
    assert notes == ["the heatmap renderer isn't converted: every feature draws plainly",
                     "a label made from an expression ($feature.NAME + ' (' + $feature.CODE + ')') isn't converted"]


def test_a_layer_without_drawing_info_or_geometry_has_no_style():
    assert to_sld({"name": "Gauges", "type": "Table"}, "gauges") is None
    assert to_sld({"name": "Rivers", "geometryType": "esriGeometryPolyline"}, "rivers") is None
