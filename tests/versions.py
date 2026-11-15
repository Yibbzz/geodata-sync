"""The GeoServer Cloud and standard GeoServer versions the e2e tests run, and the newest released.

Standard GeoServer is tested twice: the newest release, and the newest of the 2.28 series (SERIES_2),
the older series still maintained. As GeoServer ConfigOps's tests/versions.py.

    python tests/versions.py           what the repo pins, and the newest releases with images
    python tests/versions.py update    pin the newest: tests/e2e/up.sh, and the README's badges and
                                       "Tested on" line

`update` changes nothing when the repo is up to date. In GitHub Actions it writes `changed`, `branch`
and `title` to $GITHUB_OUTPUT for .github/workflows/versions.yaml, which opens a pull request and runs
CI on it: the pull request merges only when the e2e tests pass on the new versions.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(os.environ.get("VERSIONS_ROOT") or Path(__file__).resolve().parent.parent)
UP = "tests/e2e/up.sh"
README = "README.md"
TESTED = re.compile(r"<!-- tested-on -->.*?<!-- /tested-on -->", re.S)
BADGES = re.compile(r"<!-- badges -->.*?<!-- /badges -->", re.S)
SERIES_2 = "2.28"
PINS = {"cloud": "GEOSERVER_TAG", "standard": "STANDARD_GEOSERVER_VERSION", "standard-2": "STANDARD_2_GEOSERVER_VERSION"}


def get(url: str, accept: str = "application/json", method: str = "GET") -> bytes | None:
    headers = {"User-Agent": "geoserver-dataops-versions", "Accept": accept}
    if "api.github.com" in url and os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers, method=method), timeout=60) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", version))


def releases(repo: str) -> list[str]:
    """Released versions (no drafts or pre-releases), newest first, without a leading v."""
    data = json.loads(get(f"https://api.github.com/repos/{repo}/releases?per_page=50"))
    found = [r["tag_name"].removeprefix("v") for r in data if not r["draft"] and not r["prerelease"]]
    return sorted({v for v in found if re.fullmatch(r"\d+(\.\d+)+", v)}, key=key, reverse=True)


def cloud_image(version: str) -> bool:
    return get(f"https://hub.docker.com/v2/repositories/geoservercloud/geoserver-cloud-rest/tags/{version}") is not None


def standard_image(version: str) -> bool:
    return get(f"https://docker.osgeo.org/v2/geoserver/manifests/{version}",
               accept="application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json",
               method="HEAD") is not None


def newest() -> dict[str, str]:
    """The newest GeoServer Cloud and standard GeoServer (and of the 2.28 series) that have images."""
    cloud = next(v for v in releases("geoserver/geoserver-cloud") if cloud_image(v))
    standard = releases("geoserver/geoserver")
    return {"cloud": cloud,
            "standard": next(v for v in standard if standard_image(v)),
            "standard-2": next(v for v in standard if v.startswith(SERIES_2 + ".") and standard_image(v))}


def pinned() -> dict[str, str]:
    up = (ROOT / UP).read_text()
    found = {name: re.search(rf"^export {variable}=(\S+)", up, re.M).group(1) for name, variable in PINS.items()}
    found["postgis"] = re.search(r"^export POSTGIS_IMAGE=postgis/postgis:(\S+)", up, re.M).group(1)
    return found


def shield(label: str, version: str, colour: str = "brightgreen") -> str:
    """A shields.io badge; in its address a space is _ and a dash is doubled."""
    def part(text: str) -> str:
        return text.replace("-", "--").replace(" ", "_")
    return f"![{label} {version}](https://img.shields.io/badge/{part(label)}-{part(version)}-{colour})"


def badges(v: dict[str, str]) -> str:
    return ("<!-- badges -->" + " ".join([
        shield("GeoServer Cloud", v["cloud"]), shield("GeoServer", v["standard"]), shield("GeoServer", v["standard-2"]),
        shield("PostGIS", v["postgis"], "blue")]) + "<!-- /badges -->")


def tested_line(v: dict[str, str]) -> str:
    return (f"<!-- tested-on -->Tested on **GeoServer Cloud {v['cloud']}**, **GeoServer {v['standard']}** and "
            f"**GeoServer {v['standard-2']}**, with **PostGIS {v['postgis']}**: on every pull request each one "
            "publishes what `dataops sync` loaded and serves it back, before and after a reload, and a daily job "
            "(`.github/workflows/versions.yaml`) tries each new release.<!-- /tested-on -->")


def sub(path: str, pattern: str, new: str) -> None:
    file = ROOT / path
    text, count = re.subn(pattern, lambda m: new, file.read_text(), count=1, flags=re.M | re.S)
    if count != 1:
        raise SystemExit(f"{path}: {pattern!r} not found")
    file.write_text(text)


def write(v: dict[str, str]) -> None:
    for name, variable in PINS.items():
        sub(UP, rf"^export {variable}=\S+", f"export {variable}={v[name]}")
    sub(README, BADGES.pattern, badges(v))
    sub(README, TESTED.pattern, tested_line(v))


def main(argv: list[str]) -> int:
    old = pinned()
    new = {**old, **newest()}
    for name in PINS:
        mark = "" if old[name] == new[name] else "   <- newer"
        print(f"{name:12} pinned {old[name]:10} newest {new[name]}{mark}")
    changed = any(old[p] != new[p] for p in PINS) and all(key(new[p]) >= key(old[p]) for p in PINS)
    if argv[:1] == ["update"]:
        if changed:
            write(new)
            print("updated")
        else:
            print("up to date")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as out:
                out.write(f"changed={'true' if changed else 'false'}\n")
                out.write(f"branch=versions/cloud-{new['cloud']}-geoserver-{new['standard']}-{new['standard-2']}\n")
                out.write(f"title=GeoServer Cloud {new['cloud']}, GeoServer {new['standard']} and {new['standard-2']}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
