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
import unicodedata
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Timer

PORT = 8888
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "zcta.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def has_table(conn, name):
    r = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return r is not None


def normalize(s):
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()


# ── Spatial helpers ───────────────────────────────────────────────────────────

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
    polys = [geom["coordinates"]] if t == "Polygon" else geom["coordinates"] if t == "MultiPolygon" else []
    for poly in polys:
        if _ray_cast(px, py, poly[0]) and not any(_ray_cast(px, py, h) for h in poly[1:]):
            return True
    return False


# ── Query functions ───────────────────────────────────────────────────────────

def query_zips(zips):
    conn = get_conn()
    ph = ",".join("?" * len(zips))
    rows = conn.execute(f"SELECT zip, geometry FROM zcta WHERE zip IN ({ph})", zips).fetchall()
    conn.close()
    return _fc([{
        "type": "Feature",
        "properties": {"ZCTA5CE20": r["zip"], "region_type": "zip",
                       "region_id": r["zip"], "display_name": f"ZIP {r['zip']}"},
        "geometry": json.loads(r["geometry"]),
    } for r in rows])


def search_region(q):
    q = q.strip()
    conn = get_conn()
    features = []

    # ── ZIP code ──────────────────────────────────────────────────────────────
    if re.match(r"^\d{5}$", q):
        rows = conn.execute("SELECT zip, geometry FROM zcta WHERE zip=?", (q,)).fetchall()
        for r in rows:
            features.append(_feat("zip", r["zip"], f"ZIP {r['zip']}", r["geometry"]))
        conn.close()
        return _fc(features)

    # Normalize query for accent-insensitive search
    q_norm = normalize(q)
    # Strip parenthetical suffixes: "Myanmar (Burma)" → "Myanmar"
    q_base = re.sub(r"\s*\(.*?\)\s*", "", q).strip()
    q_base_norm = normalize(q_base)

    # ── Countries (Natural Earth) ─────────────────────────────────────────────
    if has_table(conn, "countries") and not features:
        q_up = q.upper()
        rows = conn.execute(
            "SELECT name, geometry FROM countries WHERE "
            "name_norm=? OR name_long_norm=? OR "
            "iso_a2=? OR iso_a3=? OR "
            "name_norm=? OR name_long_norm=?",
            (q_norm, q_norm, q_up, q_up, q_base_norm, q_base_norm)
        ).fetchall()
        for r in rows:
            features.append(_feat("country", r["name"], r["name"], r["geometry"]))

    # ── Admin1 worldwide: US states, Canadian provinces, world regions ────────
    if has_table(conn, "admin1") and not features:
        rows = conn.execute(
            "SELECT id, name, country, geometry FROM admin1 WHERE "
            "name_norm=? OR iso=?",
            (q_norm, q.upper())
        ).fetchall()
        for r in rows:
            display = f"{r['name']}, {r['country']}" if r["country"] else r["name"]
            features.append(_feat("admin1", r["id"], display, r["geometry"]))
        # If multiple (same name in different countries), keep all
        # but deduplicate identical geometries
        if len(features) > 5:
            features = features[:5]

    # ── US states table (legacy / higher-res Census data) ────────────────────
    if has_table(conn, "states") and not features:
        if re.match(r"^[A-Za-z]{2}$", q):
            rows = conn.execute("SELECT fips,name,geometry FROM states WHERE abbr=? COLLATE NOCASE", (q.upper(),)).fetchall()
        else:
            rows = conn.execute("SELECT fips,name,geometry FROM states WHERE name=? COLLATE NOCASE", (q,)).fetchall()
        for r in rows:
            features.append(_feat("state", r["fips"], r["name"], r["geometry"]))

    # ── US counties ───────────────────────────────────────────────────────────
    if has_table(conn, "counties") and not features:
        m = re.match(r"^(.+?)\s+county,?\s*([A-Za-z]{2})?$", q, re.IGNORECASE)
        if m:
            cname, st = m.group(1).strip(), (m.group(2) or "").upper()
            if st:
                rows = conn.execute(
                    "SELECT fips,namelsad,state_abbr,geometry FROM counties "
                    "WHERE name=? COLLATE NOCASE AND state_abbr=? COLLATE NOCASE", (cname, st)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT fips,namelsad,state_abbr,geometry FROM counties "
                    "WHERE name=? COLLATE NOCASE LIMIT 10", (cname,)
                ).fetchall()
        else:
            rows = conn.execute(
                "SELECT fips,namelsad,state_abbr,geometry FROM counties "
                "WHERE name=? COLLATE NOCASE LIMIT 10", (q,)
            ).fetchall()
        for r in rows:
            features.append(_feat("county", r["fips"], f"{r['namelsad']}, {r['state_abbr']}", r["geometry"]))

    conn.close()
    return _fc(features)


def get_zip_context(zip_code):
    conn = get_conn()
    row = conn.execute("SELECT cx, cy FROM zcta WHERE zip=?", (zip_code,)).fetchone()
    if not row:
        conn.close()
        return None

    px, py = row["cx"], row["cy"]
    result = {"zip": zip_code, "has_county_data": False, "has_state_data": False}

    if has_table(conn, "counties"):
        candidates = conn.execute(
            "SELECT fips,namelsad,state_fips,state_abbr,geometry FROM counties "
            "WHERE bbox_minx<=? AND bbox_maxx>=? AND bbox_miny<=? AND bbox_maxy>=?",
            (px, px, py, py)
        ).fetchall()
        for c in candidates:
            if point_in_geom(px, py, json.loads(c["geometry"])):
                result.update({
                    "county_fips": c["fips"], "county_name": c["namelsad"],
                    "state_fips": c["state_fips"], "state_abbr": c["state_abbr"],
                    "has_county_data": True
                })
                break

    if result.get("state_fips") and has_table(conn, "states"):
        s = conn.execute("SELECT name FROM states WHERE fips=?", (result["state_fips"],)).fetchone()
        if s:
            result["state_name"] = s["name"]
            result["has_state_data"] = True

    conn.close()
    return result


def _feat(rtype, rid, name, geom_json):
    return {
        "type": "Feature",
        "properties": {"region_type": rtype, "region_id": rid, "display_name": name},
        "geometry": json.loads(geom_json) if isinstance(geom_json, str) else geom_json,
    }


def _fc(features):
    return {"type": "FeatureCollection", "features": features}


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            self._file("index.html", "text/html; charset=utf-8")
        elif parsed.path == "/api/zcta":
            zips = [z.strip() for z in params.get("zips", [""])[0].split(",") if z.strip()]
            if not zips:
                return self._err(400, "No zips")
            self._ok(query_zips(zips))
        elif parsed.path == "/api/search":
            q = params.get("q", [""])[0].strip()
            if not q:
                return self._err(400, "No query")
            self._ok(search_region(q))
        elif parsed.path == "/api/zip-context":
            z = params.get("zip", [""])[0].strip().zfill(5)
            ctx = get_zip_context(z)
            if not ctx:
                return self._err(404, "Not found")
            self._ok(ctx)
        elif parsed.path == "/api/status":
            conn = get_conn()
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            info = {
                "has_zcta":      "zcta"      in tables,
                "has_states":    "states"    in tables,
                "has_counties":  "counties"  in tables,
                "has_countries": "countries" in tables,
                "has_admin1":    "admin1"    in tables,
            }
            if info["has_zcta"]:
                info["zip_count"] = conn.execute("SELECT COUNT(*) FROM zcta").fetchone()[0]
            conn.close()
            self._ok(info)
        else:
            self.send_error(404)

    def _file(self, name, ct):
        path = os.path.join(BASE_DIR, name)
        try:
            data = open(path, "rb").read()
            self._send(200, ct, data)
        except FileNotFoundError:
            self.send_error(404)

    def _ok(self, obj):
        self._send(200, "application/json", json.dumps(obj, separators=(",", ":")).encode())

    def _err(self, code, msg):
        self._send(code, "application/json", json.dumps({"error": msg}).encode())

    def _send(self, code, ct, body):
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

if __name__ == "__main__":
    os.chdir(BASE_DIR)
    if not os.path.exists(DB_PATH):
        print("❌ База данных не найдена. Запустите: python3 setup.py --file <файл>")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    n_zip  = conn.execute("SELECT COUNT(*) FROM zcta").fetchone()[0]      if "zcta"      in tables else 0
    n_cty  = conn.execute("SELECT COUNT(*) FROM countries").fetchone()[0] if "countries" in tables else 0
    n_adm  = conn.execute("SELECT COUNT(*) FROM admin1").fetchone()[0]    if "admin1"    in tables else 0
    n_co   = conn.execute("SELECT COUNT(*) FROM counties").fetchone()[0]  if "counties"  in tables else 0
    conn.close()

    mb = os.path.getsize(DB_PATH) / 1024 / 1024
    print(f"✅ База: {n_zip} ZIP  |  {n_cty} стран  |  {n_adm} регионов  |  {n_co} округов  ({mb:.0f} MB)")
    print(f"🗺  ZIP Code Map → http://localhost:{PORT}  |  Ctrl+C — стоп\n")
    Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    HTTPServer(("localhost", PORT), Handler).serve_forever()
