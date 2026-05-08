#!/usr/bin/env python3
"""
One-time setup: download US ZCTA, county and state shapefiles from Census Bureau,
convert to GeoJSON, and index into local SQLite database.
Run once; then use start.sh every time.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zcta.db")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.census.gov/",
}

SHAPEFILES = {
    "zcta": [
        "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip",
        "https://www2.census.gov/geo/tiger/GENZ2021/shp/cb_2021_us_zcta520_500k.zip",
        "https://www2.census.gov/geo/tiger/GENZ2019/shp/cb_2019_us_zcta510_500k.zip",
    ],
    "states": [
        "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_state_500k.zip",
        "https://www2.census.gov/geo/tiger/GENZ2019/shp/cb_2019_us_state_500k.zip",
    ],
    "counties": [
        "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_county_500k.zip",
        "https://www2.census.gov/geo/tiger/GENZ2019/shp/cb_2019_us_county_500k.zip",
    ],
}


# ── Dependencies ──────────────────────────────────────────────────────────────

def ensure_pyshp():
    try:
        import shapefile  # noqa: F401
        return True
    except ImportError:
        pass
    print("  Устанавливаю pyshp...")
    for cmd in ([sys.executable, "-m", "pip", "install", "pyshp"], ["pip3", "install", "pyshp"]):
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            print("  ✓ pyshp установлен")
            return True
    print("  ❌ Не удалось: pip3 install pyshp")
    return False


# ── Download ──────────────────────────────────────────────────────────────────

def download(url):
    print(f"  {os.path.basename(url)}")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        tmp = tempfile.mktemp(suffix=".zip")
        with urllib.request.urlopen(req, timeout=180) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = min(downloaded * 100 // total, 100)
                        bar = "█" * (pct // 4) + "░" * (25 - pct // 4)
                        print(f"\r  [{bar}] {pct}%  {downloaded/1024/1024:.1f} MB", end="", flush=True)
        print(f"\r  {'█'*25} 100%  {downloaded/1024/1024:.1f} MB загружено")
        return tmp
    except Exception as e:
        print(f"\n  Ошибка: {e}")
        try:
            os.remove(tmp)
        except Exception:
            pass
        return None


def try_sources(key, manual_file=None):
    if manual_file:
        if os.path.exists(manual_file):
            return manual_file
        print(f"  Файл не найден: {manual_file}")
        return None
    for url in SHAPEFILES[key]:
        path = download(url)
        if path:
            return path
    return None


# ── Geometry helpers ──────────────────────────────────────────────────────────

def get_bbox(geom):
    if geom["type"] == "Polygon":
        coords = geom["coordinates"][0]
    elif geom["type"] == "MultiPolygon":
        coords = [pt for poly in geom["coordinates"] for pt in poly[0]]
    else:
        return [0, 0, 0, 0]
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return [min(xs), min(ys), max(xs), max(ys)]


def compute_centroid(geom):
    if geom["type"] == "Polygon":
        ring = geom["coordinates"][0]
    elif geom["type"] == "MultiPolygon":
        ring = max(geom["coordinates"], key=lambda p: len(p[0]))[0]
    else:
        return [0, 0]
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return [sum(xs) / len(xs), sum(ys) / len(ys)]


# ── Shapefile reader ──────────────────────────────────────────────────────────

def read_shapefile(zip_path):
    import shapefile
    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmpdir)
        shp = next((f for f in os.listdir(tmpdir) if f.lower().endswith(".shp")), None)
        if not shp:
            raise RuntimeError("Нет .shp файла в архиве")
        sf = shapefile.Reader(os.path.join(tmpdir, shp))
        fields = [f[0] for f in sf.fields[1:]]
        records = []
        for sr in sf.shapeRecords():
            rec = dict(zip(fields, sr.record))
            try:
                geom = sr.shape.__geo_interface__
            except Exception:
                continue
            records.append((rec, geom))
        return fields, records


# ── Table builders ────────────────────────────────────────────────────────────

def build_zcta(zip_path, conn):
    print("  Читаю ZCTA shapefile...")
    fields, records = read_shapefile(zip_path)
    zip_field = next((c for c in ("ZCTA5CE20", "ZCTA5CE10", "ZCTA5", "ZIP") if c in fields), None)
    if not zip_field:
        raise RuntimeError(f"Поле ZIP не найдено. Есть: {fields}")

    conn.execute("DROP TABLE IF EXISTS zcta")
    conn.execute("""CREATE TABLE zcta (
        zip TEXT PRIMARY KEY,
        cx REAL, cy REAL,
        bbox_minx REAL, bbox_miny REAL, bbox_maxx REAL, bbox_maxy REAL,
        geometry TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zcta_zip ON zcta(zip)")

    batch = []
    for i, (rec, geom) in enumerate(records, 1):
        if i % 5000 == 0:
            print(f"\r  {i}/{len(records)}", end="", flush=True)
        z = str(rec[zip_field]).strip().zfill(5)
        cx, cy = compute_centroid(geom)
        bx = get_bbox(geom)
        batch.append((z, cx, cy, bx[0], bx[1], bx[2], bx[3], json.dumps(geom, separators=(",", ":"))))
        if len(batch) >= 2000:
            conn.executemany("INSERT OR REPLACE INTO zcta VALUES (?,?,?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        conn.executemany("INSERT OR REPLACE INTO zcta VALUES (?,?,?,?,?,?,?,?)", batch)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM zcta").fetchone()[0]
    print(f"\r  ✓ ZCTA: {n} ZIP-кодов")


def build_states(zip_path, conn):
    print("  Читаю states shapefile...")
    fields, records = read_shapefile(zip_path)
    conn.execute("DROP TABLE IF EXISTS states")
    conn.execute("""CREATE TABLE states (
        fips TEXT PRIMARY KEY,
        name TEXT, abbr TEXT,
        bbox_minx REAL, bbox_miny REAL, bbox_maxx REAL, bbox_maxy REAL,
        geometry TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_states_name ON states(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_states_abbr ON states(abbr)")

    batch = []
    for rec, geom in records:
        fips = str(rec.get("STATEFP", "")).strip()
        name = str(rec.get("NAME", "")).strip()
        abbr = str(rec.get("STUSAB", "")).strip()
        bx = get_bbox(geom)
        batch.append((fips, name, abbr, bx[0], bx[1], bx[2], bx[3], json.dumps(geom, separators=(",", ":"))))
    conn.executemany("INSERT OR REPLACE INTO states VALUES (?,?,?,?,?,?,?,?)", batch)
    conn.commit()
    print(f"  ✓ States: {len(batch)}")


def build_counties(zip_path, conn):
    print("  Читаю counties shapefile...")
    fields, records = read_shapefile(zip_path)
    conn.execute("DROP TABLE IF EXISTS counties")
    conn.execute("""CREATE TABLE counties (
        fips TEXT PRIMARY KEY,
        name TEXT, namelsad TEXT,
        state_fips TEXT, state_abbr TEXT,
        bbox_minx REAL, bbox_miny REAL, bbox_maxx REAL, bbox_maxy REAL,
        geometry TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_counties_name ON counties(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_counties_state ON counties(state_abbr)")

    batch = []
    for rec, geom in records:
        sfips = str(rec.get("STATEFP", "")).strip()
        cfips = str(rec.get("COUNTYFP", "")).strip()
        fips = sfips + cfips
        name = str(rec.get("NAME", "")).strip()
        namelsad = str(rec.get("NAMELSAD", name + " County")).strip()
        abbr = str(rec.get("STUSAB", "")).strip()
        bx = get_bbox(geom)
        batch.append((fips, name, namelsad, sfips, abbr, bx[0], bx[1], bx[2], bx[3], json.dumps(geom, separators=(",", ":"))))
    conn.executemany("INSERT OR REPLACE INTO counties VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
    conn.commit()
    print(f"  ✓ Counties: {len(batch)}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    manual_zcta = None
    if "--file" in sys.argv:
        idx = sys.argv.index("--file")
        if idx + 1 < len(sys.argv):
            manual_zcta = sys.argv[idx + 1]

    rebuild = "--rebuild" in sys.argv

    if os.path.exists(DB_PATH) and not rebuild and not manual_zcta:
        conn = sqlite3.connect(DB_PATH)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        n_zcta = conn.execute("SELECT COUNT(*) FROM zcta").fetchone()[0] if "zcta" in tables else 0
        n_states = conn.execute("SELECT COUNT(*) FROM states").fetchone()[0] if "states" in tables else 0
        n_counties = conn.execute("SELECT COUNT(*) FROM counties").fetchone()[0] if "counties" in tables else 0
        conn.close()
        print(f"✅ База уже создана: {n_zcta} ZIP, {n_states} штатов, {n_counties} округов")
        print("   Для пересоздания: python3 setup.py --rebuild")
        return True

    print("=== ZIP Code Map — первоначальная настройка ===\n")

    if not ensure_pyshp():
        print("\n❌ Установите вручную: pip3 install pyshp")
        return False

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)

    # ZCTA
    print("1/3 ZIP-коды (ZCTA):")
    zcta_zip = try_sources("zcta", manual_zcta)
    if not zcta_zip:
        print("\n❌ Скачайте вручную: https://www2.census.gov/geo/tiger/GENZ2020/shp/")
        print("   Файл: cb_2020_us_zcta520_500k.zip")
        print("   Затем: python3 setup.py --file cb_2020_us_zcta520_500k.zip")
        conn.close()
        return False
    try:
        build_zcta(zcta_zip, conn)
    finally:
        if not manual_zcta:
            try:
                os.remove(zcta_zip)
            except OSError:
                pass

    # States
    print("2/3 Штаты:")
    states_zip = try_sources("states")
    if states_zip:
        try:
            build_states(states_zip, conn)
        finally:
            try:
                os.remove(states_zip)
            except OSError:
                pass
    else:
        print("  ⚠️ States пропущены (exclusion по штатам недоступно)")

    # Counties
    print("3/3 Округа (counties):")
    counties_zip = try_sources("counties")
    if counties_zip:
        try:
            build_counties(counties_zip, conn)
        finally:
            try:
                os.remove(counties_zip)
            except OSError:
                pass
    else:
        print("  ⚠️ Counties пропущены (exclusion по округам недоступно)")

    conn.close()
    db_mb = os.path.getsize(DB_PATH) / 1024 / 1024
    print(f"\n✅ Готово!  База данных: {db_mb:.0f} MB → {DB_PATH}")
    return True


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
