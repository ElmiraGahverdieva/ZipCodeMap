#!/usr/bin/env python3
"""
One-time setup: downloads ZCTA + Natural Earth countries/provinces into SQLite.
Run once; then use start.sh every time.
  python3 setup.py                         # auto-download everything
  python3 setup.py --file cb_...zip        # supply ZCTA zip manually
  python3 setup.py --rebuild               # force full rebuild
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import urllib.request
import zipfile

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zcta.db")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

# Census Bureau (may be blocked by Cloudflare; user can supply manually)
ZCTA_URLS = [
    "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip",
    "https://www2.census.gov/geo/tiger/GENZ2021/shp/cb_2021_us_zcta520_500k.zip",
    "https://www2.census.gov/geo/tiger/GENZ2019/shp/cb_2019_us_zcta510_500k.zip",
]

# Natural Earth — hosted on AWS S3, no Cloudflare blocking
NE_COUNTRIES_URL = "https://naturalearth.s3.amazonaws.com/50m_cultural/ne_50m_admin_0_countries.zip"
NE_ADMIN1_URL    = "https://naturalearth.s3.amazonaws.com/50m_cultural/ne_50m_admin_1_states_provinces.zip"


# ── Helpers ───────────────────────────────────────────────────────────────────

def normalize(s):
    """ASCII-fold for accent-insensitive search (Côte → Cote)."""
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode().lower()


def fget(rec, *keys):
    """Case-insensitive field getter for shapefile records."""
    for k in keys:
        for variant in (k, k.upper(), k.lower()):
            v = rec.get(variant)
            if v and str(v).strip() not in ("", "-99", "-1"):
                return str(v).strip()
    return ""


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
    print(f"  ↓ {os.path.basename(url)}")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        tmp = tempfile.mktemp(suffix=".zip")
        with urllib.request.urlopen(req, timeout=180) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "html" in ct.lower():
                print("  ❌ Сервер вернул HTML (заблокировано)")
                return None
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
        print(f"\r  {'█'*25} 100%  {downloaded/1024/1024:.1f} MB")
        if downloaded < 50_000:
            print("  ❌ Файл слишком маленький")
            os.remove(tmp)
            return None
        return tmp
    except Exception as e:
        print(f"\n  Ошибка: {e}")
        try:
            os.remove(tmp)
        except Exception:
            pass
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


def build_countries(zip_path, conn):
    """Natural Earth ne_50m_admin_0_countries → countries table."""
    print("  Читаю countries shapefile...")
    fields, records = read_shapefile(zip_path)

    conn.execute("DROP TABLE IF EXISTS countries")
    conn.execute("""CREATE TABLE countries (
        name      TEXT PRIMARY KEY,
        name_norm TEXT,
        name_long TEXT,
        name_long_norm TEXT,
        admin     TEXT,
        iso_a2    TEXT,
        iso_a3    TEXT,
        geometry  TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_countries_norm  ON countries(name_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_countries_iso2  ON countries(iso_a2)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_countries_admin ON countries(admin)")

    batch = []
    seen = set()
    for rec, geom in records:
        name      = fget(rec, "ADMIN", "NAME")
        name_long = fget(rec, "NAME_LONG", "FORMAL_EN")
        iso_a2    = fget(rec, "ISO_A2")
        iso_a3    = fget(rec, "ISO_A3", "ADM0_A3")
        admin     = fget(rec, "SOVEREIGNT", "ADMIN")
        if not name or name in seen:
            continue
        seen.add(name)
        batch.append((
            name, normalize(name),
            name_long, normalize(name_long),
            admin, iso_a2.upper(), iso_a3.upper(),
            json.dumps(geom, separators=(",", ":")),
        ))
    conn.executemany("INSERT OR IGNORE INTO countries VALUES (?,?,?,?,?,?,?,?)", batch)
    conn.commit()
    print(f"  ✓ Countries: {len(batch)}")


def build_admin1(zip_path, conn):
    """Natural Earth ne_50m_admin_1_states_provinces → admin1 table.
    Covers US states, Canadian provinces, and all world subdivisions.
    """
    print("  Читаю admin1 (states/provinces) shapefile...")
    fields, records = read_shapefile(zip_path)

    conn.execute("DROP TABLE IF EXISTS admin1")
    conn.execute("""CREATE TABLE admin1 (
        id        TEXT PRIMARY KEY,
        name      TEXT,
        name_norm TEXT,
        country   TEXT,
        iso       TEXT,
        geometry  TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin1_norm    ON admin1(name_norm)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin1_country ON admin1(country)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin1_iso     ON admin1(iso)")

    batch = []
    seen = set()
    for rec, geom in records:
        adm_code = fget(rec, "adm1_code", "ADM1_CODE", "code_local")
        name     = fget(rec, "name", "NAME", "gn_name")
        country  = fget(rec, "admin", "ADMIN", "sovereignt")
        iso      = fget(rec, "iso_3166_2", "ISO_3166_2")
        if not name or not adm_code or adm_code in seen:
            continue
        seen.add(adm_code)
        batch.append((adm_code, name, normalize(name), country, iso.upper(),
                      json.dumps(geom, separators=(",", ":"))))
    conn.executemany("INSERT OR IGNORE INTO admin1 VALUES (?,?,?,?,?,?)", batch)
    conn.commit()
    print(f"  ✓ Admin1: {len(batch)} регионов")


# ── Main ──────────────────────────────────────────────────────────────────────

def table_count(conn, table):
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except Exception:
        return 0


def main():
    manual_zcta = None
    if "--file" in sys.argv:
        idx = sys.argv.index("--file")
        if idx + 1 < len(sys.argv):
            manual_zcta = sys.argv[idx + 1]

    rebuild = "--rebuild" in sys.argv

    # If DB exists, check what's already there
    if os.path.exists(DB_PATH) and not rebuild:
        conn = sqlite3.connect(DB_PATH)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        n_zcta     = table_count(conn, "zcta")
        n_countries = table_count(conn, "countries")
        n_admin1    = table_count(conn, "admin1")
        conn.close()

        if n_zcta > 0 and n_countries > 0 and n_admin1 > 0 and not manual_zcta:
            print(f"✅ База уже полная: {n_zcta} ZIP · {n_countries} стран · {n_admin1} регионов")
            print("   Для пересоздания: python3 setup.py --rebuild")
            return True

        # Partial DB — add only missing tables
        if n_zcta > 0 and not manual_zcta:
            print(f"ℹ️  ZCTA уже есть ({n_zcta} ZIP). Добавляю недостающие справочники...\n")
            if not ensure_pyshp():
                return False
            conn = sqlite3.connect(DB_PATH)
            if n_countries == 0:
                _download_and_build("4/4 Страны мира", NE_COUNTRIES_URL, build_countries, conn)
            if n_admin1 == 0:
                _download_and_build("4/4 Регионы/провинции мира", NE_ADMIN1_URL, build_admin1, conn)
            conn.close()
            db_mb = os.path.getsize(DB_PATH) / 1024 / 1024
            print(f"\n✅ Готово! База: {db_mb:.0f} MB → {DB_PATH}")
            return True

    print("=== ZIP Code Map — первоначальная настройка ===\n")

    if not ensure_pyshp():
        print("\n❌ Установите вручную: pip3 install pyshp")
        return False

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)

    # 1. ZCTA (US ZIP codes)
    print("1/4 ZIP-коды (ZCTA):")
    zcta_zip = None
    if manual_zcta:
        if os.path.exists(manual_zcta):
            zcta_zip = manual_zcta
        else:
            print(f"  Файл не найден: {manual_zcta}")
    else:
        for url in ZCTA_URLS:
            zcta_zip = download(url)
            if zcta_zip:
                break

    if not zcta_zip:
        print("\n❌ Скачайте вручную в Safari:")
        print("   https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip")
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

    # 2. Countries (Natural Earth — S3, no blocking)
    print("2/4 Страны мира (Natural Earth):")
    _download_and_build("2/4", NE_COUNTRIES_URL, build_countries, conn)

    # 3. Admin1 worldwide states/provinces (Natural Earth)
    print("3/4 Регионы/провинции мира (Natural Earth):")
    _download_and_build("3/4", NE_ADMIN1_URL, build_admin1, conn)

    # 4. US counties (Census — may be blocked)
    print("4/4 Округа США (Census Bureau):")
    county_urls = [
        "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_county_500k.zip",
        "https://www2.census.gov/geo/tiger/GENZ2019/shp/cb_2019_us_county_500k.zip",
    ]
    got_counties = False
    for url in county_urls:
        p = download(url)
        if p:
            try:
                _build_counties(p, conn)
                got_counties = True
            finally:
                try:
                    os.remove(p)
                except OSError:
                    pass
            break
    if not got_counties:
        print("  ⚠️ Округа пропущены (Census заблокирован — ок, данные стран/штатов есть)")

    conn.close()
    db_mb = os.path.getsize(DB_PATH) / 1024 / 1024
    print(f"\n✅ Готово!  База данных: {db_mb:.0f} MB → {DB_PATH}")
    return True


def _download_and_build(label, url, builder, conn):
    p = download(url)
    if p:
        try:
            builder(p, conn)
        finally:
            try:
                os.remove(p)
            except OSError:
                pass
    else:
        print(f"  ⚠️ Пропущено (ошибка загрузки)")


def _build_counties(zip_path, conn):
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_counties_name  ON counties(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_counties_state ON counties(state_abbr)")
    batch = []
    for rec, geom in records:
        sfips    = str(rec.get("STATEFP", "")).strip()
        cfips    = str(rec.get("COUNTYFP", "")).strip()
        name     = str(rec.get("NAME", "")).strip()
        namelsad = str(rec.get("NAMELSAD", name + " County")).strip()
        abbr     = str(rec.get("STUSAB", "")).strip()
        bx = get_bbox(geom)
        batch.append((sfips + cfips, name, namelsad, sfips, abbr,
                      bx[0], bx[1], bx[2], bx[3],
                      json.dumps(geom, separators=(",", ":"))))
    conn.executemany("INSERT OR REPLACE INTO counties VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
    conn.commit()
    print(f"  ✓ Counties: {len(batch)}")


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
