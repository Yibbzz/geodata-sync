"""Check a project's files against the spec, without touching the network.

Two passes: each resource against the JSON Schema (geodata_sync/schema/v1alpha1.json),
then the rules that span files: references between resources, and tables or files
two datasets would both write.
"""

from __future__ import annotations

import difflib
import json
from importlib import resources as package_files

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from .loader import Problem, Project, Resource, default_table

SOURCES_WITH_PRODUCTS = {"os-downloads", "ckan", "ogc-api-features", "wfs"}
SOURCES_WITH_ADDRESSES = {"ckan", "ogc-api-features", "wfs"}  # need the Source's spec.url


def schema() -> dict:
    return json.loads(package_files.files("geodata_sync").joinpath("schema/v1alpha1.json").read_text())


def validate(project: Project) -> list[Problem]:
    problems = list(project.problems)
    problems += check_schema(project)
    problems += check_project(project)
    problems += check_references(project)
    problems += check_tables(project)
    problems += check_unused(project)
    problems += check_rasters(project)
    return sorted(problems, key=lambda p: (str(p.file), p.line, p.severity, p.message))


# --- pass 1: each resource on its own -------------------------------------------------


def check_schema(project: Project) -> list[Problem]:
    validator = Draft202012Validator(schema())
    problems = []
    for r in project.resources:
        if not isinstance(r.data, dict):
            problems.append(Problem(r.file, r.line(), "a resource must be a mapping with apiVersion, kind, metadata and spec"))
            continue
        seen = set()
        for error in validator.iter_errors(r.data):
            if error.validator in ("if", "allOf"):
                continue  # the specific errors inside are reported on their own
            for path, text in explain(error):
                message = f"{dotted(path)}: {text}" if path else text
                if message not in seen:
                    seen.add(message)
                    problems.append(Problem(r.file, r.line(path), message, r.label))
    return problems


def explain(error) -> list[tuple[tuple, str]]:
    """(path, message) pairs in plain words for a schema error."""
    path = tuple(error.absolute_path)
    description = error.schema.get("description") if isinstance(error.schema, dict) else None
    if error.context:  # oneOf / anyOf
        if description:
            return [(path, f"{error.instance!r} isn't allowed: use {description}")]
        return explain(best_match(error.context))
    if error.validator == "additionalProperties" and isinstance(error.instance, dict):
        allowed = list((error.schema.get("properties") or {}).keys())
        out = []
        for key in sorted(set(error.instance) - set(allowed)):
            close = difflib.get_close_matches(str(key), allowed, n=1)
            hint = f" (did you mean {close[0]!r}?)" if close else f" (allowed: {', '.join(allowed)})"
            out.append((path + (key,), f"unknown field {key!r}{hint}"))
        return out
    if error.validator == "pattern" and description:
        return [(path, f"{error.instance!r} isn't allowed: use {description}")]
    if error.validator == "enum":
        return [(path, f"{error.instance!r} isn't one of: {', '.join(map(str, error.validator_value))}")]
    if error.validator == "required":
        return [(path, error.message.replace("is a required property", "is required"))]
    return [(path, error.message)]


def dotted(path: tuple) -> str:
    out = ""
    for step in path:
        out += f"[{step}]" if isinstance(step, int) else (f".{step}" if out else str(step))
    return out


# --- pass 2: rules across files ----------------------------------------------------------


# --- pass 2: rules across files ----------------------------------------------------------


def check_project(project: Project) -> list[Problem]:
    problems = []
    projects = project.of_kind("Project")
    if not projects:
        problems.append(Problem(project.root, 0, "no Project resource: every directory needs exactly one"))
    for extra in projects[1:]:
        problems.append(Problem(extra.file, extra.line(), "a second Project: every directory has exactly one", extra.label))

    for kind in ("Project", "Source", "Dataset"):
        first: dict[str, Resource] = {}
        for r in project.of_kind(kind):
            if r.name in first and kind != "Project":
                other = first[r.name]
                problems.append(Problem(r.file, r.line(("metadata", "name")),
                                        f"{kind} name already used in {other.file.name}:{other.line()}", r.label))
            first.setdefault(r.name, r)
    return problems


def check_references(project: Project) -> list[Problem]:
    problems = []
    sources = project.named("Source")

    for s in project.of_kind("Source"):
        kind = s.spec.get("type")
        if kind in SOURCES_WITH_ADDRESSES and not s.spec.get("url"):
            problems.append(Problem(s.file, s.line(("spec",)), f"spec.url: a {kind} source needs its address, e.g. https://data.gov.uk", s.label))
        if kind not in SOURCES_WITH_ADDRESSES and s.spec.get("url"):
            problems.append(Problem(s.file, s.line(("spec", "url")), f"spec.url: not used by a {kind} source", s.label))

    for d in project.of_kind("Dataset"):
        source = sources.get(d.spec.get("source"))
        if d.spec.get("source") and not source:
            problems.append(Problem(d.file, d.line(("spec", "source")), f"spec.source: no Source named {d.spec['source']!r}{known(sources)}", d.label))
        if not source:
            continue
        kind = source.spec.get("type")
        if kind in SOURCES_WITH_PRODUCTS and not d.spec.get("product"):
            problems.append(Problem(d.file, d.line(("spec",)), f"spec.product: required for a {kind} source", d.label))
        if kind == "url" and not d.spec.get("url"):
            problems.append(Problem(d.file, d.line(("spec",)), "spec.url: required for a url source", d.label))
        if kind != "url" and d.spec.get("url"):
            problems.append(Problem(d.file, d.line(("spec", "url")), f"spec.url: only used with a url source, not {kind}", d.label))
        if kind != "ckan" and d.spec.get("resource"):
            problems.append(Problem(d.file, d.line(("spec", "resource")), f"spec.resource: only used with a ckan source, not {kind}", d.label))

    return problems


def check_tables(project: Project) -> list[Problem]:
    """No two Datasets load the same table."""
    problems = []
    tables: dict[str, Resource] = {}
    for d in project.of_kind("Dataset"):
        for key, options in (d.spec.get("tables") or {}).items():
            table = (options or {}).get("name") or default_table(d.name or "", key)
            if table in tables and tables[table] is not d:
                other = tables[table]
                problems.append(Problem(d.file, d.line(("spec", "tables", key)),
                                        f"spec.tables.{key}: table {table} is also loaded by {other.label}", d.label))
            tables.setdefault(table, d)
    return problems


def check_unused(project: Project) -> list[Problem]:
    """Warnings: things declared but never used."""
    problems = []
    used_sources = {d.spec.get("source") for d in project.of_kind("Dataset")}
    for s in project.of_kind("Source"):
        if s.name not in used_sources:
            problems.append(Problem(s.file, s.line(), "no Dataset uses this source", s.label, "warning"))
    return problems


def known(named: dict) -> str:
    return f" (known: {', '.join(sorted(named))})" if named else ""


def check_rasters(project: Project) -> list[Problem]:
    """A Dataset loads tables or a raster, not both; no two raster Datasets write one file."""
    problems, paths = [], {}
    for d in project.of_kind("Dataset"):
        if "raster" not in d.spec:
            continue
        if "tables" in d.spec:
            problems.append(Problem(d.file, d.line(("spec", "raster")), "spec.raster: a Dataset loads tables or a raster, not both", d.label))
        path = d.spec["raster"].get("path")
        if path in paths:
            problems.append(Problem(d.file, d.line(("spec", "raster", "path")), f"spec.raster.path: {path} is also written by {paths[path].label}", d.label))
        paths.setdefault(path, d)
    return problems
