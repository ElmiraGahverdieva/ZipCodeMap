#!/usr/bin/env python3
"""
ZIP Code Map — local server.
Serves index.html and queries the local SQLite database.
Run setup.py first if zcta.db doesn't exist.
"""
import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Timer

PORT = 8888
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "zcta.db")


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def has_table(conn, name):
    r = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return r is not None


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _ray_cast(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def point_in_geom(px, py, geom):
    t = geom["type"]
    if t == "Polygon":
        polys = [geom["coordinates"]]
    elif t == "MultiPolygon":
        polys = geom["coordinates"]
    else:
        return False
    for poly in polys:
        if _ray_cast(px, py, poly[0]):
            if not any(_ray_cast(px, py, h) for h in poly[1:]):
                return True
    return False


# ── Query functions ───────────────────────────────────────────────────────────

def query_zips(zips):
    conn = get_conn()
    ph = ",".join("?" * len(zips))
    rows = conn.execute(f"SELECT zip, geometry FROM zcta WHERE zip IN ({ph})", zips).fetchall()
    conn.close()
    return _make_fc([{
        "type": "Feature",
        "properties": {"ZCTA5CE20": r["zip"], "region_type": "zip", "region_id": r["zip"],
                       "display_name": f"ZIP {r['zip']}"},
        "geometry": json.loads(r["geometry"]),
    } for r in rows])


def search_region(q):
    """Search states, counties, and ZIPs by name/abbreviation/code."""
    q = q.strip()
    conn = get_conn()
    features = []

    # ZIP code
    if re.match(r"^\d{5}$", q):
        rows = conn.execute("SELECT zip, geometry FROM zcta WHERE zip=?", (q,)).fetchall()
        for r in rows:
            features.append(_region_feature("zip", r["zip"], f"ZIP {r['zip']}", r["geometry"]))

    # State abbreviation (2 letters)
    elif re.match(r"^[A-Za-z]{2}$", q) and has_table(conn, "states"):
        rows = conn.execute("SELECT fips, name, abbr, geometry FROM states WHERE abbr=? COLLATE NOCASE", (q.upper(),)).fetchall()
        for r in rows:
            features.append(_region_feature("state", r["fips"], r["name"], r["geometry"]))

    # State full name
    elif has_table(conn, "states"):
        # Try exact state name match
        rows = conn.execute("SELECT fips, name, abbr, geometry FROM states WHERE name=? COLLATE NOCASE", (q,)).fetchall()
        if rows:
            for r in rows:
                features.append(_region_feature("state", r["fips"], r["name"], r["geometry"]))
        elif has_table(conn, "counties"):
            # Try county match: "Allegheny County, PA" or "Allegheny County"
            county_match = re.match(r"^(.+?)\s+county,?\s*([A-Za-z]{2})?$", q, re.IGNORECASE)
            if county_match:
                cname = county_match.group(1).strip()
                state_abbr = (county_match.group(2) or "").upper()
                if state_abbr:
                    rows = conn.execute(
                        "SELECT fips, namelsad, state_abbr, geometry FROM counties "
                        "WHERE name=? COLLATE NOCASE AND state_abbr=? COLLATE NOCASE",
                        (cname, state_abbr)
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT fips, namelsad, state_abbr, geometry FROM counties "
                        "WHERE name=? COLLATE NOCASE",
                        (cname,)
                    ).fetchall()
                for r in rows:
                    label = f"{r['namelsad']}, {r['state_abbr']}"
                    features.append(_region_feature("county", r["fips"], label, r["geometry"]))
            else:
                # Try county partial match (no "County" suffix)
                rows = conn.execute(
                    "SELECT fips, namelsad, state_abbr, geometry FROM counties "
                    "WHERE name=? COLLATE NOCASE LIMIT 10",
                    (q,)
                ).fetchall()
                for r in rows:
                    label = f"{r['namelsad']}, {r['state_abbr']}"
                    features.append(_region_feature("county", r["fips"], label, r["geometry"]))

    conn.close()
    return _make_fc(features)


def get_zip_context(zip_code):
    """Return county and state info for a given ZIP, using centroid point-in-polygon."""
    conn = get_conn()
    row = conn.execute("SELECT cx, cy FROM zcta WHERE zip=?", (zip_code,)).fetchone()
    if not row:
        conn.close()
        return None

    px, py = row["cx"], row["cy"]
    result = {"zip": zip_code}

    if has_table(conn, "counties"):
        # Pre-filter by bounding box, then do exact PiP
        candidates = conn.execute(
            "SELECT fips, namelsad, state_fips, state_abbr, geometry FROM counties "
            "WHERE bbox_minx<=? AND bbox_maxx>=? AND bbox_miny<=? AND bbox_maxy>=?",
            (px, px, py, py)
        ).fetchall()
        for c in candidates:
            geom = json.loads(c["geometry"])
            if point_in_geom(px, py, geom):
                result["county_fips"] = c["fips"]
                result["county_name"] = c["namelsad"]
                result["state_fips"] = c["state_fips"]
                result["state_abbr"] = c["state_abbr"]
                break

    if "state_fips" in result and has_table(conn, "states"):
        s = conn.execute("SELECT fips, name, geometry FROM states WHERE fips=?", (result["state_fips"],)).fetchone()
        if s:
            result["state_name"] = s["name"]
            result["state_geometry"] = s["geometry"]

    if "county_fips" in result:
        c = conn.execute("SELECT geometry FROM counties WHERE fips=?", (result["county_fips"],)).fetchone()
        if c:
            result["county_geometry"] = c["geometry"]

    conn.close()
    return result


def _region_feature(rtype, rid, display_name, geometry_json):
    return {
        "type": "Feature",
        "properties": {
            "region_type": rtype,
            "region_id": rid,
            "display_name": display_name,
        },
        "geometry": json.loads(geometry_json) if isinstance(geometry_json, str) else geometry_json,
    }


def _make_fc(features):
    return {"type": "FeatureCollection", "features": features}


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")

        elif parsed.path == "/api/zcta":
            zips = [z.strip() for z in params.get("zips", [""])[0].split(",") if z.strip()]
            if not zips:
                return self._json_err(400, "No ZIP codes")
            self._json_ok(query_zips(zips))

        elif parsed.path == "/api/search":
            q = params.get("q", [""])[0].strip()
            if not q:
                return self._json_err(400, "No query")
            self._json_ok(search_region(q))

        elif parsed.path == "/api/zip-context":
            zip_code = params.get("zip", [""])[0].strip().zfill(5)
            if not zip_code:
                return self._json_err(400, "No ZIP")
            ctx = get_zip_context(zip_code)
            if not ctx:
                return self._json_err(404, f"ZIP {zip_code} not found")
            self._json_ok(ctx)

        else:
            self.send_error(404)

    def _serve_file(self, filename, ct):
        path = os.path.join(BASE_DIR, filename)
        try:
            data = open(path, "rb").read()
            self._respond(200, ct, data)
        except FileNotFoundError:
            self.send_error(404)

    def _json_ok(self, obj):
        body = json.dumps(obj, separators=(",", ":")).encode()
        self._respond(200, "application/json", body)

    def _json_err(self, code, msg):
        body = json.dumps({"error": msg}).encode()
        self._respond(code, "application/json", body)

    def _respond(self, code, ct, body):
        self.send_response(code)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        if args and len(args) > 1 and str(args[1]) >= "400":
            print(f"  [{args[1]}] {args[0]}", file=sys.stderr)


# ── Entry point ───────────────────────────────────────────────────────────────

def check_db():
    if not os.path.exists(DB_PATH):
        print("❌ База данных не найдена. Запустите: python3 setup.py")
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    n_zip = conn.execute("SELECT COUNT(*) FROM zcta").fetchone()[0] if "zcta" in tables else 0
    n_st = conn.execute("SELECT COUNT(*) FROM states").fetchone()[0] if "states" in tables else 0
    n_co = conn.execute("SELECT COUNT(*) FROM counties").fetchone()[0] if "counties" in tables else 0
    conn.close()
    print(f"✅ База: {n_zip} ZIP  |  {n_st} штатов  |  {n_co} округов")


if __name__ == "__main__":
    os.chdir(BASE_DIR)
    check_db()
    server = HTTPServer(("localhost", PORT), Handler)
    db_mb = os.path.getsize(DB_PATH) / 1024 / 1024
    print(f"🗺  ZIP Code Map → http://localhost:{PORT}  ({db_mb:.0f} MB)  |  Ctrl+C — стоп\n")
    Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен.")
