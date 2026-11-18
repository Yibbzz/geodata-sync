# Your own source types (plugins)

dataops reads many kinds of source itself (docs/spec.md lists them), and files in any folder or bucket
(`type: files`). For anything else (your organisation's API, a supplier's portal) you can write a source
type in a Python package of your own and install it beside dataops. A Source names it by its `type`,
and `validate`, `discover` and `sync` treat it like a built-in one. Nothing in dataops changes.

[`plugin-example/`](plugin-example/) is a complete one, for the USGS earthquake catalogue: one module and
a `pyproject.toml`.

## The package

A class, and one entry point in the group `geoserver_dataops.sources` naming it:

```toml
[project.entry-points."geoserver_dataops.sources"]
usgs-earthquakes = "dataops_usgs_earthquakes:Earthquakes"
```

The name on the left is the `type` a Source uses. It can't replace a built-in type. `pip install` the
package where dataops runs (in a container image: `RUN pip install ./my-source` after the dataops image's
`FROM`), and check with `dataops discover <type>`. A plugin that fails to load is left out with a note;
`validate` then reports Sources using it as an unknown type.

## The class

```python
from geoserver_dataops.sources import Download, Listing, NotFound, Product, SourceError, http_session

class MySource:
    name = "My API"                      # how messages name it
    formats = ["GeoJSON"]                # what it loads, best first

    def __init__(self, api_key=None, url=None, session=None, credentials=None, options=None): ...
    def products(self, search=None, limit=None) -> Listing: ...      # discover <source>
    def product(self, product_id, resource=None) -> Product: ...     # discover <source> <product>, sync
    def download_url(self, download: Download) -> str: ...           # where the file is
```

| Part | What dataops gives or asks |
| --- | --- |
| `__init__` | `url`: the Source's `spec.url`. `credentials`: a function, a key of the Source's secret → its value (`credentials("apiKey")`; `None` when it isn't set), from the environment, secrets.yaml or a mounted Secret, as for built-in types. `options`: the Source's `spec.options`. `api_key`: the secret's `apiKey`, for older types. Take only the arguments you need: dataops passes `credentials` and `options` only to a class that accepts them |
| `products` | The products to list, filtered by `search` (the helper `matches(product, search)` does it), at most `limit`. Raise `SourceError` if there's no list ("give the product's ID") |
| `product` | One product, with its `downloads` and the one sync loads as `chosen`. Raise `NotFound` for an unknown ID |
| `Product.version` | What says the data changed: a date, a revision, an ETag. A Dataset is loaded again only when it differs from the last load's. `None`: no version; sync downloads the file and loads it when its contents changed |
| `download_url` | The file's address, for `discover` (which reads layers from it in ranges where the server allows) and for `sync` |

And two optional methods:

| Method | When |
| --- | --- |
| `selection(product_id, options) -> Product` | The Dataset's `spec.options` change what is fetched (an area, a date range, a filter). Called by `sync` instead of `product` |
| `fetch(download, dest) -> str` | Getting the file takes more than a GET of `download_url` (a POST, several pages, a token to fetch first), or the answer holds something that changes every time (a timestamp) and should be left out. Write the file to `dest` and return its SHA-256 |

Errors meant for the person running dataops are `SourceError("…")`, in plain words; credentials in any
address in the message are masked. Anything else is reported as a bug in the plugin, and only that
Dataset fails.

A `Download` is one file: `format` (GeoJSON, GeoPackage, Shapefile, ZIP, GeoTIFF…), `url`, `file_name`
(its extension tells GDAL how to read it, and a single-file layer is named after it), `size`, `md5` (checked
after download), `loadable`. `id` and `name` are what `resource` matches when a product has several.

## The files

```yaml
apiVersion: dataops/v1alpha1
kind: Source
metadata: { name: quakes }
spec:
  type: usgs-earthquakes
  credentials: { secret: usgs }      # if the type reads any: credentials("apiKey") and so on
  options: { region: us }            # passed to the class as `options`
---
apiVersion: dataops/v1alpha1
kind: Dataset
metadata: { name: big-quakes }
spec:
  source: quakes
  product: "4.5"
  options: { days: 7 }               # passed to selection()
  update: daily
```

`validate` checks a plugin Source's type exists, and leaves its `url`, `product` and `options` to the
plugin, which reports problems when `discover` or `sync` runs.

## Testing one

Test against a fake of the API on a local port, as dataops's own tests do (`tests/fake_*.py`), then
against the real API: `dataops discover`, `dataops sync` into a scratch database, and `sync` again to see
it skip unchanged data.
