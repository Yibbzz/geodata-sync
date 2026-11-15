"""A small Overture bucket on a local port, for the overture source's tests: S3 listings and Parquet files read
with HTTP range requests, as the real bucket serves them. Release 2026-09-23.0 and 2026-10-21.0; type
places/place in two files: one near (0, 51) in two row groups, one far away (100, 0)."""

import http.server
import re
import threading
import urllib.parse
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import shapely

RELEASES = ("2026-09-23.0", "2026-10-21.0")


def places(points):
    """A table as Overture writes one: id, WKB geometry, nested names, a list, and a bbox struct."""
    geometries = [shapely.Point(x, y) for _, x, y in points]
    return pa.table({
        "id": [p[0] for p in points],
        "geometry": pa.array(shapely.to_wkb(geometries).tolist(), pa.binary()),
        "names": pa.array([{"primary": f"place {p[0]}"} for p in points], pa.struct([("primary", pa.string())])),
        "categories": pa.array([["cafe"] for _ in points], pa.list_(pa.string())),
        "bbox": pa.array([{"xmin": x, "xmax": x, "ymin": y, "ymax": y} for _, x, y in points],
                         pa.struct([(k, pa.float32()) for k in ("xmin", "xmax", "ymin", "ymax")])),
    })


class OvertureBucket:
    def __init__(self, folder: Path):
        self.files, self.ranges = {}, []
        near = [("a", 0.10, 51.10), ("b", 0.20, 51.20), ("c", 0.90, 51.90), ("d", 5.0, 55.0)]  # d: in the file, not the area
        for release in RELEASES:
            prefix = f"release/{release}/theme=places/type=place/"
            for name, rows, group in (("part-0.parquet", near, 2), ("part-1.parquet", [("far", 100.0, 0.0)], 1)):
                path = folder / f"{release}-{name}"
                pq.write_table(places(rows), path, row_group_size=group)  # two row groups: a, b | c, d
                self.files[prefix + name] = path.read_bytes()
            self.files[f"release/{release}/theme=buildings/type=building/part-0.parquet"] = b"PAR1"
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                if url.path == "/":
                    return self.send(200, server.listing(dict(urllib.parse.parse_qsl(url.query))).encode(), {})
                data = server.files.get(url.path.lstrip("/"))
                if data is None:
                    return self.send(404, b"NoSuchKey", {})
                m = re.match(r"bytes=(\d+)-(\d+)", self.headers.get("Range", ""))
                if not m:
                    return self.send(200, data, {})
                start, end = int(m.group(1)), min(int(m.group(2)), len(data) - 1)
                server.ranges.append((url.path, start, end))
                self.send(206, data[start:end + 1], {"Content-Range": f"bytes {start}-{end}/{len(data)}"})

            def send(self, status, body, headers):
                self.send_response(status)
                for k, v in {"Content-Length": str(len(body)), **headers}.items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()

    def listing(self, q):
        prefix, delimiter = q.get("prefix", ""), q.get("delimiter")
        keys = sorted(k for k in self.files if k.startswith(prefix))
        folders, contents = set(), []
        for k in keys:
            rest = k[len(prefix):]
            if delimiter and delimiter in rest:
                folders.add(prefix + rest.split(delimiter)[0] + delimiter)
            else:
                contents.append(k)
        body = "".join(f"<CommonPrefixes><Prefix>{f}</Prefix></CommonPrefixes>" for f in sorted(folders))
        body += "".join(f"<Contents><Key>{k}</Key><Size>{len(self.files[k])}</Size></Contents>" for k in contents)
        return ('<?xml version="1.0" encoding="UTF-8"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                f"<Prefix>{prefix}</Prefix><IsTruncated>false</IsTruncated>{body}</ListBucketResult>")
