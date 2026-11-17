"""A small Overpass API on a local port, for the overpass source's tests. It answers any query with the
same OSM XML (two pubs: a node, and a building way with its nodes), stamped with the current time as
Overpass does; `busy` answers 429, and `extra` adds a third pub."""

import http.server
import threading
import time
from urllib.parse import parse_qs


class OverpassServer:
    def __init__(self):
        self.queries, self.busy, self.extra, self.user_agents = [], False, False, []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                server.queries.append(parse_qs(body).get("data", [""])[0])
                server.user_agents.append(self.headers.get("User-Agent"))
                if server.busy:
                    return self.send(429, b"Too many requests")
                if not self.headers.get("User-Agent") or self.headers["User-Agent"].startswith("python-requests"):
                    return self.send(406, b"Not acceptable: say who you are")
                self.send(200, server.osm().encode())

            def send(self, status, body):
                self.send_response(status)
                self.send_header("Content-Type", "application/osm3s+xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/api/interpreter"

    def close(self):
        self.httpd.shutdown()

    def osm(self) -> str:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + len(self.queries)))  # never the same twice
        extra = '<node id="5" lat="52.67" lon="-0.73"><tag k="amenity" v="pub"/><tag k="name" v="The Third"/></node>' \
            if self.extra else ""
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<osm version="0.6" generator="Overpass API test">
<note>The data included in this document is from www.openstreetmap.org. The data is made available under ODbL.</note>
<meta osm_base="{stamp}"/>
  <node id="1" lat="52.670" lon="-0.720"><tag k="amenity" v="pub"/><tag k="name" v="The Wheatsheaf"/></node>
  <node id="2" lat="52.671" lon="-0.721"/>
  <node id="3" lat="52.671" lon="-0.722"/>
  <node id="4" lat="52.672" lon="-0.722"/>
  {extra}
  <way id="10"><nd ref="2"/><nd ref="3"/><nd ref="4"/><nd ref="2"/><tag k="amenity" v="pub"/><tag k="building" v="yes"/><tag k="name" v="The Odd House"/></way>
</osm>
"""
