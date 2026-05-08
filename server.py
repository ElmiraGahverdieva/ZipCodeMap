#!/usr/bin/env python3
"""
ZIP Code Map — local proxy server.
Serves index.html and proxies Census Bureau TIGERweb requests to avoid CORS.
No external dependencies — uses Python stdlib only.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Timer

PORT = 8888
TIGER_BASE = (
    "https://tigerweb.geo.census.gov/arcgis/rest/services/"
    "TIGERweb/PUMA_TAD_TAZ_UGA_ZCTA/MapServer/2/query"
)


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")

        elif parsed.path == "/api/zcta":
            params = urllib.parse.parse_qs(parsed.query)
            zips_raw = params.get("zips", [""])[0]
            zips = [z.strip() for z in zips_raw.split(",") if z.strip()]
            if not zips:
                self._json_error(400, "No ZIP codes provided")
                return
            self._proxy_zcta(zips)

        else:
            self.send_error(404)

    def _proxy_zcta(self, zips):
        quoted = ",".join(f"'{z}'" for z in zips)
        where = f"ZCTA5CE20 IN ({quoted})"
        query = urllib.parse.urlencode({
            "where": where,
            "outFields": "ZCTA5CE20",
            "outSR": "4326",
            "f": "geojson",
        })
        url = f"{TIGER_BASE}?{query}"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ZipCodeMap/1.0"})
            with urllib.request.urlopen(req, timeout=45) as resp:
                data = resp.read()

            # Validate we got real GeoJSON
            parsed_json = json.loads(data)
            if "error" in parsed_json:
                msg = parsed_json["error"].get("message", "Census API error")
                self._json_error(502, msg)
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        except urllib.error.URLError as e:
            self._json_error(502, f"Cannot reach Census Bureau API: {e.reason}")
        except Exception as e:
            self._json_error(500, str(e))

    def _serve_file(self, filename, content_type):
        path = os.path.join(os.path.dirname(__file__), filename)
        try:
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def _json_error(self, code, message):
        body = json.dumps({"error": message}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # Only log errors
        if args and str(args[1]) >= "400":
            print(f"  [{args[1]}] {args[0]}", file=sys.stderr)


def open_browser():
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    server = HTTPServer(("localhost", PORT), Handler)
    print(f"ZIP Code Map запущен → http://localhost:{PORT}")
    print("Нажмите Ctrl+C для остановки\n")
    Timer(1.2, open_browser).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
