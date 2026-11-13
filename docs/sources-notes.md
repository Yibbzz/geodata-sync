# Notes on data sources

What we learned loading open data.

- Big layers over WFS or OGC API Features don't load reliably: servers that page by `startIndex` get slower the deeper the page, and drop pages. Use a file download when there is one: 415,000 flood zone features never loaded over OGC API; the same data as a 1 GB GeoPackage loaded in 8 minutes.
- GDAL retries HTTP 429, 502, 503 and 504 (`GDAL_HTTP_MAX_RETRY`, `GDAL_HTTP_RETRY_DELAY`), but not broken connections.
- GeoJSON always counts as WGS84.
- data.gov.uk's CKAN API answers plain `curl` with HTML; send an `Accept` header.
