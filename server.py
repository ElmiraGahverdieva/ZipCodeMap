#!/usr/bin/env python3
"""
ZIP Code Map — local server.
Serves index.html and queries the local ZCTA SQLite database (no external API).
Run setup.py first if zcta.db doesn't exist.
"""
import json
import os
import sqlite3
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Timer

PORT = 8888
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "zcta.db")


def check_db():
    if not os.path.exists(DB_PATH):
        print("❌ База данных не найдена.")
        print("   Сначала запустите: python3 setup.py")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    n = conn.execute("SELECT COUNT(*) FROM zcta").fetchone()[0]
    conn.close()
    print(f"✅ База данных: {n} ZIP-кодов")


def query_zips(zips):
    conn = sqlite3.connect(DB_PATH)
    placeholders = ",".join("?" * len(zips))
    rows = conn.execute(
        f"SELECT zip, geometry FROM zcta WHERE zip IN ({placeholders})",
        zips
    ).fetchall()
    conn.close()

    features = [
        {
            "type": "Feature",
            "properties": {"ZCTA5CE20": row[0]},
            "geometry": json.loads(row[1]),
        }
        for row in rows
    ]
    return {"type": "FeatureCollection", "features": features}


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
                self._respond(400, "application/json", b'{"error":"No ZIP codes"}')
                return
            try:
                geojson = query_zips(zips)
                body = json.dumps(geojson, separators=(",", ":")).encode()
                self._respond(200, "application/json", body)
            except Exception as e:
                body = json.dumps({"error": str(e)}).encode()
                self._respond(500, "application/json", body)

        else:
            self.send_error(404)

    def _serve_file(self, filename, content_type):
        path = os.path.join(BASE_DIR, filename)
        try:
            with open(path, "rb") as f:
                data = f.read()
            self._respond(200, content_type, data)
        except FileNotFoundError:
            self.send_error(404)

    def _respond(self, code, content_type, body):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if args and len(args) > 1 and str(args[1]) >= "400":
            print(f"  [{args[1]}] {args[0]}", file=sys.stderr)


def open_browser():
    webbrowser.open(f"http://localhost:{PORT}")


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    check_db()
    server = HTTPServer(("localhost", PORT), Handler)
    db_mb = os.path.getsize(DB_PATH) / 1024 / 1024
    print(f"🗺  ZIP Code Map → http://localhost:{PORT}")
    print(f"   База: {db_mb:.0f} MB  |  Ctrl+C для остановки\n")
    Timer(1.0, open_browser).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
