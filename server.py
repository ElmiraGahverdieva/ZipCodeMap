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
import threading
import unicodedata
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Timer

PORT = 8888
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "zcta.db")

# Census state FIPS → 2-letter abbreviation (used to patch empty state_abbr in counties table)
_STATE_FIPS_TO_ABBR = {
    '01':'AL','02':'AK','04':'AZ','05':'AR','06':'CA','08':'CO','09':'CT',
    '10':'DE','11':'DC','12':'FL','13':'GA','15':'HI','16':'ID','17':'IL',
    '18':'IN','19':'IA','20':'KS','21':'KY','22':'LA','23':'ME','24':'MD',
    '25':'MA','26':'MI','27':'MN','28':'MS','29':'MO','30':'MT','31':'NE',
    '32':'NV','33':'NH','34':'NJ','35':'NM','36':'NY','37':'NC','38':'ND',
    '39':'OH','40':'OK','41':'OR','42':'PA','44':'RI','45':'SC','46':'SD',
    '47':'TN','48':'TX','49':'UT','50':'VT','51':'VA','53':'WA','54':'WV',
    '55':'WI','56':'WY',
}


_has_table_cache = {}

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA cache_size=-32000")
    return conn


def has_table(conn, name):
    if name not in _has_table_cache:
        r = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
        _has_table_cache[name] = r is not None
    return _has_table_cache[name]


def normalize(s):
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()


# ── Nielsen DMA® matching ─────────────────────────────────────────────────────
# DMA labels look like "Knoxville, TN - KY" or "Tri - Cities, TN - VA - KY" —
# city name(s) followed by every state the market spans. We match on the
# normalized city text plus how many of the query's state abbreviations
# overlap with the label's, since the same city can be the lead name of
# slightly different-looking labels across data sources.
_dma_index_cache = None


def _norm_dma_text(s):
    s = re.sub(r"[-,]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _dma_index(conn):
    global _dma_index_cache
    if _dma_index_cache is None:
        idx = []
        if has_table(conn, "dma_counties"):
            for r in conn.execute("SELECT DISTINCT dma_label FROM dma_counties").fetchall():
                label = r["dma_label"]
                abbrs = set(re.findall(r"\b[A-Z]{2}\b", label))
                city_only = re.sub(r"\b[A-Z]{2}\b", " ", label)
                idx.append({"label": label, "abbrs": abbrs, "city_norm": _norm_dma_text(city_only)})
        _dma_index_cache = idx
    return _dma_index_cache


def _find_dma_label(conn, city_query, state_abbrs):
    city_q = _norm_dma_text(city_query)
    if not city_q:
        return None
    state_set = {a.strip().upper() for a in state_abbrs if a.strip()}
    best, best_score = None, -1
    for entry in _dma_index(conn):
        if city_q in entry["city_norm"] or entry["city_norm"] in city_q:
            score = len(entry["abbrs"] & state_set)
            if score > best_score:
                best, best_score = entry["label"], score
    return best


def query_dma(conn, city, state_abbrs):
    label = _find_dma_label(conn, city, state_abbrs)
    if not label:
        return None
    rows = conn.execute(
        "SELECT county_name, state_abbr FROM dma_counties WHERE dma_label=?", (label,)
    ).fetchall()
    features = []
    if rows and has_table(conn, "counties"):
        parts, params = [], []
        for r in rows:
            parts.append("(name=? COLLATE NOCASE AND state_abbr=? COLLATE NOCASE)")
            params.extend([r["county_name"], r["state_abbr"]])
        crows = conn.execute(
            "SELECT fips,namelsad,state_abbr,geometry FROM counties WHERE " + " OR ".join(parts),
            params
        ).fetchall()
        for c in crows:
            features.append(_feat("dma", c["fips"], f"{label} DMA", c["geometry"]))
    return {"label": label, "fc": _fc(features)}


def query_dma_batch(items):
    """items=[{city, states:[...]}, ...] → dict "city|ST,ST" → {label, fc}."""
    if not items:
        return {}
    conn = get_conn()
    if not has_table(conn, "dma_counties"):
        conn.close()
        return {}
    result = {}
    for item in items:
        city = str(item.get("city", "")).strip()
        states = item.get("states", [])
        if not isinstance(states, list):
            states = [states]
        key = f"{city.lower()}|{','.join(sorted(s.upper() for s in states if s))}"
        if key in result or not city:
            continue
        r = query_dma(conn, city, states)
        if r:
            result[key] = r
    conn.close()
    return result


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
    """Direct ZCTA match first; any ZIP with no polygon of its own falls back
    to its parent ZCTA via the HRSA ZIP→ZCTA crosswalk (~41k USPS ZIPs vs.
    ~33.8k with their own ZCTA — the rest are PO boxes/unique-recipient/
    sparse-rural ZIPs that legitimately don't have a distinct Census area)."""
    conn = get_conn()
    ph = ",".join("?" * len(zips))
    rows = conn.execute(f"SELECT zip, geometry FROM zcta WHERE zip IN ({ph})", zips).fetchall()
    geoms = {r["zip"]: r["geometry"] for r in rows}
    approx = set()

    missing = [z for z in zips if z not in geoms]
    if missing and has_table(conn, "zip_crosswalk"):
        ph2 = ",".join("?" * len(missing))
        cw_rows = conn.execute(
            f"SELECT zip, zcta FROM zip_crosswalk WHERE zip IN ({ph2})", missing
        ).fetchall()
        needed = {r["zcta"] for r in cw_rows if r["zcta"] not in geoms}
        if needed:
            ph3 = ",".join("?" * len(needed))
            zcta_rows = conn.execute(
                f"SELECT zip, geometry FROM zcta WHERE zip IN ({ph3})", list(needed)
            ).fetchall()
            parent_geom = {r["zip"]: r["geometry"] for r in zcta_rows}
            for r in cw_rows:
                if r["zcta"] in parent_geom:
                    geoms[r["zip"]] = parent_geom[r["zcta"]]
                    approx.add(r["zip"])
    conn.close()

    features = []
    for zip_, geom in geoms.items():
        props = {"ZCTA5CE20": zip_, "region_type": "zip",
                  "region_id": zip_, "display_name": f"ZIP {zip_}"}
        if zip_ in approx:
            props["approx"] = True
        features.append({"type": "Feature", "properties": props, "geometry": json.loads(geom)})
    return _fc(features)


def _us_state_abbr(conn, st_raw):
    """2-letter abbreviation passthrough, or look up a full US state name via admin1."""
    st_raw = st_raw.strip()
    if len(st_raw) == 2:
        return st_raw.upper()
    if not st_raw or not has_table(conn, "admin1"):
        return ""
    row = conn.execute(
        "SELECT iso FROM admin1 WHERE name=? COLLATE NOCASE "
        "AND country='United States of America'", (st_raw,)
    ).fetchone()
    return row["iso"].split("-")[1] if row else ""


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

    # ── 2-letter US state abbreviation — resolve via admin1 ISO code first ─────
    # Half of all US state abbreviations collide with an ISO-3166-1 alpha-2
    # country code (GA→Gabon, PA→Panama, IN→India, DE→Germany, ...). The
    # frontend already commits to treating a bare 2-letter code as a US state
    # when it's a known state abbreviation, so resolve it unambiguously here
    # before the generic country/admin1 name search below can pick the wrong one.
    if re.match(r"^[A-Za-z]{2}$", q) and has_table(conn, "admin1"):
        rows = conn.execute(
            "SELECT id, name, country, geometry FROM admin1 WHERE iso=?",
            (f"US-{q.upper()}",)
        ).fetchall()
        for r in rows:
            features.append(_feat("state", r["id"], r["name"], r["geometry"]))
        if features:
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
        m = re.match(r"^(.+?)\s+(?:county|parish|borough),?\s*([A-Za-z]{2,})?$", q, re.IGNORECASE)
        if m:
            cname, st_raw = m.group(1).strip(), (m.group(2) or "").strip()
            st = _us_state_abbr(conn, st_raw)
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

    # ── US county subdivisions (townships, precincts, New England towns) ──────
    # Two shapes: an explicit "X Township/Precinct, State" (mirrors the county
    # branch above), and a bare "Name, State" fallback — many townships are
    # pasted without a suffix keyword at all (e.g. "Peach Bottom, Pennsylvania"),
    # so this is tried last, only once nothing more specific has matched.
    if has_table(conn, "cousub") and not features:
        m = re.match(r"^(.+?)\s+(?:township|precinct),?\s*([A-Za-z]{2,})?$", q, re.IGNORECASE)
        if m:
            cname, st_raw = m.group(1).strip(), (m.group(2) or "").strip()
            st = _us_state_abbr(conn, st_raw)
            if st:
                rows = conn.execute(
                    "SELECT fips,namelsad,state_abbr,geometry FROM cousub "
                    "WHERE name=? COLLATE NOCASE AND state_abbr=? COLLATE NOCASE", (cname, st)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT fips,namelsad,state_abbr,geometry FROM cousub "
                    "WHERE name=? COLLATE NOCASE LIMIT 10", (cname,)
                ).fetchall()
        else:
            # Bare "Name, State" — no "Township"/"Precinct" keyword to signal
            # intent, so only accept it when the name is unambiguous within
            # the state. Township/precinct names repeat heavily within a
            # state (e.g. Indiana alone has 17 different "Franklin"
            # townships; MS/LA number some precincts "1".."82") — with no
            # county given there is no way to pick the right one, so
            # returning all of them would silently spam the map with
            # townships nobody asked for. Ambiguous → fall through to
            # Nominatim/place resolution instead of guessing.
            bare = [p.strip() for p in q.split(",")]
            rows = []
            if len(bare) == 2:
                st = _us_state_abbr(conn, bare[1])
                if st:
                    candidates = conn.execute(
                        "SELECT fips,namelsad,state_abbr,geometry FROM cousub "
                        "WHERE name=? COLLATE NOCASE AND state_abbr=? COLLATE NOCASE", (bare[0], st)
                    ).fetchall()
                    if len(candidates) == 1:
                        rows = candidates
        for r in rows:
            features.append(_feat("cousub", r["fips"], f"{r['namelsad']}, {r['state_abbr']}", r["geometry"]))

    conn.close()
    return _fc(features)


def get_zip_context(zip_code):
    conn = get_conn()
    row = conn.execute("SELECT cx, cy FROM zcta WHERE zip=?", (zip_code,)).fetchone()
    if not row and has_table(conn, "zip_crosswalk"):
        # This ZIP has no ZCTA of its own — use its parent ZCTA's centroid
        # (HRSA ZIP→ZCTA crosswalk) as an approximation for county/state context.
        cw = conn.execute("SELECT zcta FROM zip_crosswalk WHERE zip=?", (zip_code,)).fetchone()
        if cw:
            row = conn.execute("SELECT cx, cy FROM zcta WHERE zip=?", (cw["zcta"],)).fetchone()
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


def _patch_county_state_abbr(conn):
    """One-time fix: setup.py imported STUSAB (wrong field) instead of STUSPS,
    leaving state_abbr empty for all counties. Populate it from state_fips."""
    if not has_table(conn, "counties"):
        return
    empty = conn.execute(
        "SELECT COUNT(*) FROM counties WHERE state_abbr='' OR state_abbr IS NULL"
    ).fetchone()[0]
    if empty == 0:
        return
    print(f"  🔧 Патч counties.state_abbr ({empty} строк)…", flush=True)
    for fips, abbr in _STATE_FIPS_TO_ABBR.items():
        conn.execute(
            "UPDATE counties SET state_abbr=? "
            "WHERE state_fips=? AND (state_abbr='' OR state_abbr IS NULL)",
            (abbr, fips)
        )
    conn.commit()
    print("  ✓ state_abbr исправлен", flush=True)


def query_counties_batch(items):
    """Batch county lookup: items=[{name, state}, ...] → dict "name:state" → feature."""
    if not items:
        return {}
    conn = get_conn()
    if not has_table(conn, "counties"):
        conn.close()
        return {}
    result = {}
    CHUNK = 200  # stay within SQLite expression depth limits
    for offset in range(0, len(items), CHUNK):
        chunk = items[offset:offset + CHUNK]
        parts, params = [], []
        for item in chunk:
            name  = item.get("name", "")
            state = item.get("state", "").upper()
            if state:
                parts.append("(name=? COLLATE NOCASE AND state_abbr=? COLLATE NOCASE)")
                params.extend([name, state])
            else:
                parts.append("(name=? COLLATE NOCASE)")
                params.append(name)
        rows = conn.execute(
            "SELECT fips,name,namelsad,state_abbr,geometry FROM counties WHERE " + " OR ".join(parts),
            params
        ).fetchall()
        for r in rows:
            n = r["name"].lower()
            s = r["state_abbr"].lower()
            feat = _feat("county", r["fips"], f"{r['namelsad']}, {r['state_abbr']}", r["geometry"])
            result.setdefault(f"{n}:{s}", feat)
            result.setdefault(f"{n}:", feat)
    conn.close()
    return result


def query_cd_batch(items):
    """items=[{state_fips:"13", cd_fp:"02"}, ...] → dict "state_fips:cd_fp" → feature."""
    if not items:
        return {}
    conn = get_conn()
    if not has_table(conn, "congressional_districts"):
        conn.close()
        return {}
    result = {}
    CHUNK = 200
    for offset in range(0, len(items), CHUNK):
        chunk = items[offset:offset + CHUNK]
        parts, params = [], []
        for item in chunk:
            sfips = str(item.get("state_fips", "")).zfill(2)
            cdfp  = str(item.get("cd_fp", "")).zfill(2)
            parts.append("(state_fips=? AND cd_fp=?)")
            params.extend([sfips, cdfp])
        rows = conn.execute(
            "SELECT state_fips,cd_fp,namelsad,geometry FROM congressional_districts WHERE "
            + " OR ".join(parts), params
        ).fetchall()
        for r in rows:
            key  = f"{r['state_fips']}:{r['cd_fp']}"
            feat = _feat("congressional_district", key, r["namelsad"], r["geometry"])
            result[key] = feat
    conn.close()
    return result


def area_at(lat, lon, mode="county"):
    """Return county + state (mode=county) or ZIP + county + state context
    (mode=zip) at a given lat/lon, for the Show All Areas hover feature."""
    conn = get_conn()
    px, py = float(lon), float(lat)
    result = {}

    # Province mode doesn't use county/state context (its tooltip only shows
    # the province + country) — skip this lookup there, it's dead work.
    if mode != "province" and has_table(conn, "counties"):
        candidates = conn.execute(
            "SELECT fips,name,namelsad,state_fips,state_abbr,geometry FROM counties "
            "WHERE bbox_minx<=? AND bbox_maxx>=? AND bbox_miny<=? AND bbox_maxy>=?",
            (px, px, py, py)
        ).fetchall()
        for c in candidates:
            geom = json.loads(c["geometry"])
            if point_in_geom(px, py, geom):
                abbr = c["state_abbr"] or _STATE_FIPS_TO_ABBR.get(c["state_fips"], "")
                result = {
                    "county_fips":  c["fips"],
                    "county_name":  c["namelsad"],
                    "county_short": c["name"],
                    "state_fips":   c["state_fips"],
                    "state_abbr":   abbr,
                    "county_geom":  geom,
                }
                break

    if result.get("state_abbr") and has_table(conn, "admin1"):
        row = conn.execute(
            "SELECT name FROM admin1 WHERE iso=? AND country='United States of America'",
            (f"US-{result['state_abbr']}",)
        ).fetchone()
        if row:
            result["state_name"] = row["name"]

    if mode == "zip" and has_table(conn, "zcta"):
        zrows = conn.execute(
            "SELECT zip,geometry FROM zcta "
            "WHERE bbox_minx<=? AND bbox_maxx>=? AND bbox_miny<=? AND bbox_maxy>=?",
            (px, px, py, py)
        ).fetchall()
        for z in zrows:
            geom = json.loads(z["geometry"])
            if point_in_geom(px, py, geom):
                result["zip"] = z["zip"]
                result["zip_geom"] = geom
                break

    if mode == "province" and has_table(conn, "admin1"):
        prows = conn.execute(
            "SELECT id,name,country,geometry FROM admin1 "
            "WHERE bbox_minx<=? AND bbox_maxx>=? AND bbox_miny<=? AND bbox_maxy>=?",
            (px, px, py, py)
        ).fetchall()
        for p in prows:
            geom = json.loads(p["geometry"])
            if point_in_geom(px, py, geom):
                result["province_id"]      = p["id"]
                result["province_name"]    = p["name"]
                result["province_country"] = p["country"]
                result["province_geom"]    = geom
                break

    if mode == "cd" and has_table(conn, "congressional_districts"):
        crows = conn.execute(
            "SELECT state_fips,cd_fp,namelsad,geometry FROM congressional_districts "
            "WHERE bbox_minx<=? AND bbox_maxx>=? AND bbox_miny<=? AND bbox_maxy>=?",
            (px, px, py, py)
        ).fetchall()
        for c in crows:
            geom = json.loads(c["geometry"])
            if point_in_geom(px, py, geom):
                abbr = _STATE_FIPS_TO_ABBR.get(c["state_fips"], "")
                state_row = conn.execute(
                    "SELECT name FROM admin1 WHERE iso=? AND country='United States of America'",
                    (f"US-{abbr}",)
                ).fetchone() if abbr and has_table(conn, "admin1") else None
                result["cd_state_fips"] = c["state_fips"]
                result["cd_fp"]         = c["cd_fp"]
                result["cd_namelsad"]   = c["namelsad"]
                result["cd_state_abbr"] = abbr
                result["cd_state_name"] = state_row["name"] if state_row else ""
                result["cd_geom"]       = geom
                break

    conn.close()
    return result or None


def area_all(mode, min_lon, min_lat, max_lon, max_lat, limit=1500):
    """All zones of the given mode whose bbox overlaps the viewport — lets
    the frontend paint every ZIP/county/CD/province in view at once (light
    highlight) instead of the user hunting for boundaries one hover at a
    time. Approximate (bbox overlap, not exact polygon-viewport intersection)
    since this is a visual aid, not a precise computation — cheap even for a
    few hundred candidates."""
    conn = get_conn()
    features = []
    truncated = False

    def _bbox_rows(table, cols):
        return conn.execute(
            f"SELECT {cols} FROM {table} "
            "WHERE bbox_minx<=? AND bbox_maxx>=? AND bbox_miny<=? AND bbox_maxy>=? "
            "LIMIT ?",
            (max_lon, min_lon, max_lat, min_lat, limit + 1)
        ).fetchall()

    if mode == "zip" and has_table(conn, "zcta"):
        rows = _bbox_rows("zcta", "zip,geometry")
        truncated = len(rows) > limit
        for r in rows[:limit]:
            features.append(_feat("zip", r["zip"], f"ZIP {r['zip']}", r["geometry"]))

    elif mode == "county" and has_table(conn, "counties"):
        rows = _bbox_rows("counties", "fips,namelsad,state_abbr,geometry")
        truncated = len(rows) > limit
        for r in rows[:limit]:
            features.append(_feat("county", r["fips"], f"{r['namelsad']}, {r['state_abbr']}", r["geometry"]))

    elif mode == "cd" and has_table(conn, "congressional_districts"):
        rows = _bbox_rows("congressional_districts", "state_fips,cd_fp,namelsad,geometry")
        truncated = len(rows) > limit
        for r in rows[:limit]:
            key = f"{r['state_fips']}:{r['cd_fp']}"
            features.append(_feat("congressional_district", key, r["namelsad"], r["geometry"]))

    elif mode == "province" and has_table(conn, "admin1"):
        rows = _bbox_rows("admin1", "id,name,country,geometry")
        truncated = len(rows) > limit
        for r in rows[:limit]:
            display = f"{r['name']}, {r['country']}" if r["country"] else r["name"]
            features.append(_feat("admin1", r["id"], display, r["geometry"]))

    conn.close()
    return {"fc": _fc(features), "truncated": truncated}


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
        elif parsed.path == "/api/search-batch":
            # Accepts multiple q= params, returns {query: FeatureCollection, ...}
            queries = [q.strip() for q in params.get("q", []) if q.strip()]
            result = {q: search_region(q) for q in queries[:300]}
            self._ok(result)
        elif parsed.path == "/api/zip-context":
            z = params.get("zip", [""])[0].strip().zfill(5)
            ctx = get_zip_context(z)
            if not ctx:
                return self._err(404, "Not found")
            self._ok(ctx)
        elif parsed.path == "/api/area-at":
            try:
                lat = float(params.get("lat", [None])[0])
                lon = float(params.get("lon", [None])[0])
            except (TypeError, ValueError):
                return self._err(400, "lat and lon required")
            mode = params.get("mode", ["county"])[0]
            data = area_at(lat, lon, mode)
            self._ok(data if data else {})
        elif parsed.path == "/api/area-all":
            try:
                min_lat = float(params.get("min_lat", [None])[0])
                max_lat = float(params.get("max_lat", [None])[0])
                min_lon = float(params.get("min_lon", [None])[0])
                max_lon = float(params.get("max_lon", [None])[0])
            except (TypeError, ValueError):
                return self._err(400, "min_lat/max_lat/min_lon/max_lon required")
            mode = params.get("mode", ["county"])[0]
            self._ok(area_all(mode, min_lon, min_lat, max_lon, max_lat))
        elif parsed.path == "/api/status":
            conn = get_conn()
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            info = {
                "has_zcta":      "zcta"                    in tables,
                "has_states":    "states"                  in tables,
                "has_counties":  "counties"                in tables,
                "has_countries": "countries"               in tables,
                "has_admin1":    "admin1"                  in tables,
                "has_cd":        "congressional_districts" in tables,
                "has_dma":       "dma_counties"             in tables,
                "has_cousub":    "cousub"                   in tables,
                "has_zip_crosswalk": "zip_crosswalk"         in tables,
            }
            if info["has_zcta"]:
                info["zip_count"] = conn.execute("SELECT COUNT(*) FROM zcta").fetchone()[0]
            if info["has_cd"]:
                info["cd_count"] = conn.execute("SELECT COUNT(*) FROM congressional_districts").fetchone()[0]
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

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/api/counties-batch", "/api/cd-batch", "/api/dma-batch"):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                items = json.loads(body)
                if not isinstance(items, list):
                    return self._err(400, "Expected JSON array")
                if parsed.path == "/api/cd-batch":
                    self._ok(query_cd_batch(items))
                elif parsed.path == "/api/dma-batch":
                    self._ok(query_dma_batch(items))
                else:
                    self._ok(query_counties_batch(items))
            except Exception as e:
                self._err(400, str(e))
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        if args and len(args) > 1 and str(args[1]) >= "400":
            print(f"  [{args[1]}] {args[0]}", file=sys.stderr)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    os.chdir(BASE_DIR)
    if not os.path.exists(DB_PATH):
        print("❌ База данных не найдена. Запустите: python setup.py --file <файл>")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    # Pre-populate has_table cache so request handlers skip sqlite_master queries
    _has_table_cache.update({t: True for t in tables})
    _patch_county_state_abbr(conn)
    n_zip  = conn.execute("SELECT COUNT(*) FROM zcta").fetchone()[0]                        if "zcta"                    in tables else 0
    n_cty  = conn.execute("SELECT COUNT(*) FROM countries").fetchone()[0]                  if "countries"               in tables else 0
    n_adm  = conn.execute("SELECT COUNT(*) FROM admin1").fetchone()[0]                     if "admin1"                  in tables else 0
    n_co   = conn.execute("SELECT COUNT(*) FROM counties").fetchone()[0]                   if "counties"                in tables else 0
    n_cd   = conn.execute("SELECT COUNT(*) FROM congressional_districts").fetchone()[0]    if "congressional_districts" in tables else 0
    n_dma  = conn.execute("SELECT COUNT(DISTINCT dma_label) FROM dma_counties").fetchone()[0] if "dma_counties"             in tables else 0
    n_cs   = conn.execute("SELECT COUNT(*) FROM cousub").fetchone()[0]                     if "cousub"                  in tables else 0
    conn.close()

    mb = os.path.getsize(DB_PATH) / 1024 / 1024
    print(f"✅ База: {n_zip} ZIP  |  {n_cty} стран  |  {n_adm} регионов  |  {n_co} окр.  |  {n_cd} округов Конгресса  |  {n_dma} DMA  |  {n_cs} подразделений округов  ({mb:.0f} MB)")
    print(f"🗺  ZIP Code Map → http://localhost:{PORT}  |  Ctrl+C — стоп\n")
    Timer(1.0, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    ThreadingHTTPServer(("localhost", PORT), Handler).serve_forever()
