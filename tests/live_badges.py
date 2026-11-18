"""The README's source badges from a run of the live checks: one shields.io endpoint file per source,
`passing` or `failing` (or `not run`), written by .github/workflows/sources.yaml to the `badges` branch.

    python tests/live_badges.py OUT_DIR RESULTS.xml...      (JUnit XML, from pytest --junitxml)
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# badge id -> (label, the tests that decide it: test_live_sources' case, or a test's name)
BADGES = {
    "ckan": "CKAN", "socrata": "Socrata", "opendatasoft": "OpenDataSoft", "arcgis": "ArcGIS Server and Online",
    "arcgis-hub": "ArcGIS Hub", "csw": "CSW (GeoNetwork)", "atom": "INSPIRE Atom", "os-downloads": "Ordnance Survey",
    "wfs": "WFS", "ogc-api-features": "OGC API Features", "stac-earth-search": "STAC (Earth Search)",
    "stac-planetary-computer": "STAC (Planetary Computer)", "overture": "Overture Maps", "geofabrik": "OpenStreetMap (Geofabrik)",
    "overpass": "OpenStreetMap (Overpass)", "url": "A link", "files-s3": "Amazon S3", "files-gcs": "Google Cloud Storage",
    "files-azure": "Azure Blob Storage", "files-folder": "A folder or share", "files-sftp": "SFTP", "files-ftp": "FTP",
    "database-postgresql": "PostgreSQL / PostGIS", "database-mysql": "MySQL / MariaDB",
}
# tests elsewhere that stand for a badge
OTHER_TESTS = {"test_a_plain_path_is_a_folder": "files-folder", "test_sftp_lists_and_fetches_with_a_pinned_key": "files-sftp",
               "test_ftp_lists_relative_to_the_login_and_fetches": "files-ftp",
               "test_a_postgresql_table_is_copied_and_again_only_when_it_changes": "database-postgresql",
               "test_a_mariadb_table_is_copied": "database-mysql"}


def results(paths: list[str]) -> dict[str, str]:
    """badge id -> passing, failing or not run."""
    states: dict[str, list[str]] = {}
    for path in paths:
        for case in ET.parse(path).getroot().iter("testcase"):
            name = case.get("name", "")
            m = re.fullmatch(r"test_live_source\[(.+)\]", name)
            badge = m.group(1) if m else OTHER_TESTS.get(name)
            if badge not in BADGES:
                continue
            failed = case.find("failure") is not None or case.find("error") is not None
            states.setdefault(badge, []).append("failing" if failed else "not run" if case.find("skipped") is not None else "passing")
    # several tests for one badge: any failure fails it
    found = {}
    for badge, seen in states.items():
        found[badge] = "failing" if "failing" in seen else "passing" if "passing" in seen else "not run"
    return found


def main(out: str, paths: list[str]) -> int:
    folder = Path(out)
    folder.mkdir(parents=True, exist_ok=True)
    found = results(paths)
    colors = {"passing": "brightgreen", "failing": "red", "not run": "lightgrey"}
    for badge, label in BADGES.items():
        state = found.get(badge, "not run")
        (folder / f"{badge}.json").write_text(json.dumps(
            {"schemaVersion": 1, "label": label, "message": state, "color": colors[state]}) + "\n")
        print(f"{badge}: {state}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2:]))
