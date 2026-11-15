"""tests/versions.py: the pinned GeoServer versions, and writing new ones to up.sh and the README."""

import shutil
from pathlib import Path

import versions

REPO = Path(__file__).resolve().parent.parent


def copy(tmp_path, monkeypatch):
    for path in (versions.UP, versions.README):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / path, tmp_path / path)
    monkeypatch.setattr(versions, "ROOT", tmp_path)
    return tmp_path


def test_the_readme_names_the_pinned_versions():
    """What `update` would write for the versions up.sh pins is what the README has."""
    readme, pinned = (REPO / versions.README).read_text(), versions.pinned()
    assert versions.badges(pinned) in readme and versions.tested_line(pinned) in readme


def test_update_pins_new_versions_everywhere(tmp_path, monkeypatch):
    root = copy(tmp_path, monkeypatch)
    new = {**versions.pinned(), "cloud": "9.1.0.2", "standard": "9.1.0", "standard-2": "2.28.99"}
    versions.write(new)
    assert versions.pinned() == new
    readme = (root / versions.README).read_text()
    assert "badge/GeoServer_Cloud-9.1.0.2-brightgreen" in readme and "badge/GeoServer-2.28.99-brightgreen" in readme
    assert "badge/PostGIS-18--3.7-blue" in readme  # a dash in a badge's text is doubled
    assert "**GeoServer Cloud 9.1.0.2**, **GeoServer 9.1.0** and **GeoServer 2.28.99**" in readme
    assert readme.count("<!-- tested-on -->") == 1 and readme.count("<!-- badges -->") == 1


def test_only_newer_versions_with_images_are_pinned(tmp_path, monkeypatch, capsys):
    copy(tmp_path, monkeypatch)
    old = versions.pinned()
    listed = {"geoserver/geoserver-cloud": ["9.0.0", old["cloud"]], "geoserver/geoserver": ["9.0.0", old["standard"], old["standard-2"]]}
    monkeypatch.setattr(versions, "releases", lambda repo: listed[repo])
    monkeypatch.setattr(versions, "cloud_image", lambda v: v != "9.0.0")  # released, its image not yet pushed
    monkeypatch.setattr(versions, "standard_image", lambda v: True)
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert versions.main(["update"]) == 0
    assert versions.pinned() == {**old, "standard": "9.0.0"}
    assert f"changed=true\nbranch=versions/cloud-{old['cloud']}-geoserver-9.0.0-{old['standard-2']}\n" in output.read_text()
    assert versions.main(["update"]) == 0 and "up to date" in capsys.readouterr().out
