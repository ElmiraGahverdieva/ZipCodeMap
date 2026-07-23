#!/usr/bin/env python3
"""
One-time setup: downloads ZCTA + Natural Earth countries/provinces into SQLite.
Run once; then use start.bat (Windows) every time.
  python setup.py                         # auto-download everything
  python setup.py --file cb_...zip        # supply ZCTA zip manually
  python setup.py --rebuild               # force full rebuild
  python setup.py --cousub                # add county subdivisions (townships/precincts)
  python setup.py --cd                    # add congressional districts + admin1 bbox migration
"""
import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import urllib.request
import zipfile

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zcta.db")
DMA_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nielsen_dma_counties.csv")
ZIP_CROSSWALK_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zip_zcta_crosswalk.csv")

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

# County subdivisions (townships, precincts, boroughs-as-subdivision, New
# England towns, ...) — unlike ZCTA/counties/states, Census only publishes
# this layer one state at a time, so building it means 51 separate downloads.
COUSUB_URL_TMPL = "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_{fips}_cousub_500k.zip"

# Congressional districts (116th Congress) — single national file, no
# per-state downloads needed (unlike county subdivisions above).
CD_URL = "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_cd116_500k.zip"
STATE_FIPS_TO_ABBR = {
    '01':'AL','02':'AK','04':'AZ','05':'AR','06':'CA','08':'CO','09':'CT',
    '10':'DE','11':'DC','12':'FL','13':'GA','15':'HI','16':'ID','17':'IL',
    '18':'IN','19':'IA','20':'KS','21':'KY','22':'LA','23':'ME','24':'MD',
    '25':'MA','26':'MI','27':'MN','28':'MS','29':'MO','30':'MT','31':'NE',
    '32':'NV','33':'NH','34':'NJ','35':'NM','36':'NY','37':'NC','38':'ND',
    '39':'OH','40':'OK','41':'OR','42':'PA','44':'RI','45':'SC','46':'SD',
    '47':'TN','48':'TX','49':'UT','50':'VT','51':'VA','53':'WA','54':'WV',
    '55':'WI','56':'WY',
}


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
    for cmd in ([sys.executable, "-m", "pip", "install", "pyshp"], ["pip", "install", "pyshp"]):
        if subprocess.run(cmd, capture_output=True).returncode == 0:
            print("  ✓ pyshp установлен")
            return True
    print("  ❌ Не удалось: pip install pyshp")
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
        sf.close()  # release .shp/.dbf/.shx handles before TemporaryDirectory
                    # tries to delete them — required on Windows, which
                    # (unlike Unix) refuses to remove a file that's still open
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zcta_bbox ON zcta(bbox_minx, bbox_maxx, bbox_miny, bbox_maxy)")

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

    if "--cousub" in sys.argv:
        if not os.path.exists(DB_PATH):
            print("❌ zcta.db не найдена. Сначала запустите: python setup.py")
            return False
        if not ensure_pyshp():
            return False
        print("Округа-подразделения (townships/precincts), 51 файл по штатам:\n")
        conn = sqlite3.connect(DB_PATH)
        build_cousub(conn)
        conn.close()
        db_mb = os.path.getsize(DB_PATH) / 1024 / 1024
        print(f"\n✅ Готово! База: {db_mb:.0f} MB → {DB_PATH}")
        return True

    if "--cd" in sys.argv:
        if not os.path.exists(DB_PATH):
            print("❌ zcta.db не найдена. Сначала запустите: python setup.py")
            return False
        if not ensure_pyshp():
            return False
        print("Округа Конгресса (единый национальный файл):\n")
        conn = sqlite3.connect(DB_PATH)
        _download_and_build("CD", CD_URL, build_congressional_districts, conn)
        migrate_admin1_bbox(conn)
        conn.close()
        db_mb = os.path.getsize(DB_PATH) / 1024 / 1024
        print(f"\n✅ Готово! База: {db_mb:.0f} MB → {DB_PATH}")
        return True

    # If DB exists, check what's already there
    if os.path.exists(DB_PATH) and not rebuild:
        conn = sqlite3.connect(DB_PATH)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        n_zcta     = table_count(conn, "zcta")
        n_countries = table_count(conn, "countries")
        n_admin1    = table_count(conn, "admin1")
        n_dma       = table_count(conn, "dma_counties")
        n_zxwalk    = table_count(conn, "zip_crosswalk")
        admin1_has_bbox = "bbox_minx" in {r[1] for r in conn.execute("PRAGMA table_info(admin1)").fetchall()}
        conn.close()

        if (n_zcta > 0 and n_countries > 0 and n_admin1 > 0 and n_dma > 0 and n_zxwalk > 0
                and admin1_has_bbox and not manual_zcta):
            print(f"✅ База уже полная: {n_zcta} ZIP · {n_countries} стран · {n_admin1} регионов · {n_dma} DMA-записей")
            print("   Для пересоздания: python setup.py --rebuild")
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
            if n_dma == 0:
                print("  Nielsen DMA (рынки телевещания):")
                build_dma_counties(conn)
            if n_zxwalk == 0:
                print("  ZIP→ZCTA crosswalk (заполнение пробелов):")
                build_zip_crosswalk(conn)
            if not admin1_has_bbox and table_count(conn, "admin1") > 0:
                migrate_admin1_bbox(conn)
            conn.close()
            db_mb = os.path.getsize(DB_PATH) / 1024 / 1024
            print(f"\n✅ Готово! База: {db_mb:.0f} MB → {DB_PATH}")
            return True

    print("=== ZIP Code Map — первоначальная настройка ===\n")

    if not ensure_pyshp():
        print("\n❌ Установите вручную: pip install pyshp")
        return False

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = sqlite3.connect(DB_PATH)

    # 1. ZCTA (US ZIP codes)
    print("1/6 ZIP-коды (ZCTA):")
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
        print("   Затем: python setup.py --file cb_2020_us_zcta520_500k.zip")
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
    print("2/6 Страны мира (Natural Earth):")
    _download_and_build("2/6", NE_COUNTRIES_URL, build_countries, conn)

    # 3. Admin1 worldwide states/provinces (Natural Earth)
    print("3/6 Регионы/провинции мира (Natural Earth):")
    _download_and_build("3/6", NE_ADMIN1_URL, build_admin1, conn)

    # 4. US counties (Census — may be blocked)
    print("4/6 Округа США (Census Bureau):")
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

    # 5. Nielsen DMA® (TV media markets) — bundled crosswalk, no download
    print("5/6 Nielsen DMA (рынки телевещания):")
    build_dma_counties(conn)

    # 6. ZIP→ZCTA crosswalk — bundled, fills gaps for ZIPs without their own ZCTA
    print("6/6 ZIP→ZCTA crosswalk (заполнение пробелов):")
    build_zip_crosswalk(conn)

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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_counties_bbox ON counties(bbox_minx, bbox_maxx, bbox_miny, bbox_maxy)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_counties_state ON counties(state_abbr)")
    batch = []
    for rec, geom in records:
        sfips    = str(rec.get("STATEFP", "")).strip()
        cfips    = str(rec.get("COUNTYFP", "")).strip()
        name     = str(rec.get("NAME", "")).strip()
        namelsad = str(rec.get("NAMELSAD", name + " County")).strip()
        abbr     = str(rec.get("STUSPS", rec.get("STUSAB", ""))).strip()
        bx = get_bbox(geom)
        batch.append((sfips + cfips, name, namelsad, sfips, abbr,
                      bx[0], bx[1], bx[2], bx[3],
                      json.dumps(geom, separators=(",", ":"))))
    conn.executemany("INSERT OR REPLACE INTO counties VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
    conn.commit()
    print(f"  ✓ Counties: {len(batch)}")


def build_congressional_districts(zip_path, conn):
    """Congressional districts (116th Congress) → congressional_districts
    table. Single national file, same shape as counties (bbox columns for
    fast point-in-polygon pre-filtering during map-hover lookups)."""
    print("  Читаю congressional districts shapefile...")
    fields, records = read_shapefile(zip_path)
    conn.execute("DROP TABLE IF EXISTS congressional_districts")
    conn.execute("""CREATE TABLE congressional_districts (
        state_fips TEXT, cd_fp TEXT, namelsad TEXT,
        bbox_minx REAL, bbox_miny REAL, bbox_maxx REAL, bbox_maxy REAL,
        geometry TEXT,
        PRIMARY KEY (state_fips, cd_fp)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cd_state ON congressional_districts(state_fips)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cd_bbox ON congressional_districts(bbox_minx, bbox_maxx, bbox_miny, bbox_maxy)")
    batch = []
    for rec, geom in records:
        sfips    = str(rec.get("STATEFP", "")).strip()
        cdfp     = str(rec.get("CD116FP", "")).strip()
        namelsad = str(rec.get("NAMELSAD", "")).strip()
        bx = get_bbox(geom)
        batch.append((sfips, cdfp, namelsad, bx[0], bx[1], bx[2], bx[3],
                      json.dumps(geom, separators=(",", ":"))))
    conn.executemany("INSERT OR REPLACE INTO congressional_districts VALUES (?,?,?,?,?,?,?,?)", batch)
    conn.commit()
    print(f"  ✓ Congressional districts: {len(batch)}")


def migrate_admin1_bbox(conn):
    """Add bbox columns to admin1 (world provinces/US states, Natural Earth)
    and backfill them, so province-level map hover can use the same fast
    bbox pre-filter as counties/zcta/cousub instead of scanning every
    polygon on the planet per mousemove."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(admin1)").fetchall()}
    if "bbox_minx" in cols:
        return
    print("  Добавляю bbox-колонки в admin1...")
    conn.execute("ALTER TABLE admin1 ADD COLUMN bbox_minx REAL")
    conn.execute("ALTER TABLE admin1 ADD COLUMN bbox_miny REAL")
    conn.execute("ALTER TABLE admin1 ADD COLUMN bbox_maxx REAL")
    conn.execute("ALTER TABLE admin1 ADD COLUMN bbox_maxy REAL")
    rows = conn.execute("SELECT id, geometry FROM admin1").fetchall()
    batch = []
    for rid, geom_json in rows:
        bx = get_bbox(json.loads(geom_json))
        batch.append((bx[0], bx[1], bx[2], bx[3], rid))
    conn.executemany(
        "UPDATE admin1 SET bbox_minx=?, bbox_miny=?, bbox_maxx=?, bbox_maxy=? WHERE id=?",
        batch
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_admin1_bbox ON admin1(bbox_minx, bbox_maxx, bbox_miny, bbox_maxy)")
    conn.commit()
    print(f"  ✓ admin1: bbox добавлен ({len(batch)} регионов)")


def build_cousub(conn):
    """County subdivisions (townships, precincts, boroughs-as-subdivision,
    New England towns, Census County Divisions in states without legal
    subdivisions). Census only publishes this one state at a time, so this
    downloads 51 small files (50 states + DC) instead of one national file.
    A failed/blocked state is skipped, not fatal — partial coverage is fine.
    """
    conn.execute("DROP TABLE IF EXISTS cousub")
    conn.execute("""CREATE TABLE cousub (
        fips TEXT PRIMARY KEY,
        name TEXT, namelsad TEXT,
        state_fips TEXT, state_abbr TEXT,
        bbox_minx REAL, bbox_miny REAL, bbox_maxx REAL, bbox_maxy REAL,
        geometry TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cousub_name  ON cousub(name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cousub_bbox ON cousub(bbox_minx, bbox_maxx, bbox_miny, bbox_maxy)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cousub_state ON cousub(state_abbr)")

    ok, failed = 0, []
    items = sorted(STATE_FIPS_TO_ABBR.items())
    for i, (fips, abbr) in enumerate(items, 1):
        print(f"  [{i}/{len(items)}] {abbr}...", end=" ", flush=True)
        url = COUSUB_URL_TMPL.format(fips=fips)
        p = download_quiet(url)
        if not p:
            print("❌")
            failed.append(abbr)
            continue
        try:
            fields, records = read_shapefile(p)
            batch = []
            for rec, geom in records:
                sfips    = str(rec.get("STATEFP", fips)).strip()
                cfips    = str(rec.get("COUSUBFP", "")).strip()
                name     = str(rec.get("NAME", "")).strip()
                namelsad = str(rec.get("NAMELSAD", name)).strip()
                bx = get_bbox(geom)
                batch.append((sfips + cfips, name, namelsad, sfips, abbr,
                              bx[0], bx[1], bx[2], bx[3],
                              json.dumps(geom, separators=(",", ":"))))
            conn.executemany("INSERT OR REPLACE INTO cousub VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
            conn.commit()
            print(f"✓ {len(batch)}")
            ok += 1
        except Exception as e:
            print(f"❌ ({e})")
            failed.append(abbr)
        finally:
            try:
                os.remove(p)
            except OSError:
                pass

    n = conn.execute("SELECT COUNT(*) FROM cousub").fetchone()[0]
    print(f"  ✓ County subdivisions: {n} ({ok}/{len(items)} штатов)")
    if failed:
        print(f"  ⚠️ Пропущены (блокировка/ошибка): {', '.join(failed)}")


def download_quiet(url):
    """Like download(), but without the progress bar — used for the 51
    small per-state county-subdivision files where a bar per file is noise."""
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        tmp = tempfile.mktemp(suffix=".zip")
        with urllib.request.urlopen(req, timeout=60) as resp:
            ct = resp.headers.get("Content-Type", "")
            if "html" in ct.lower():
                return None
            data = resp.read()
        if len(data) < 500:
            return None
        with open(tmp, "wb") as f:
            f.write(data)
        return tmp
    except Exception:
        return None


def build_zip_crosswalk(conn):
    """HRSA GeoCare Navigator ZIP-to-ZCTA crosswalk (successor to the UDS
    Mapper crosswalk), bundled in the repo (no download needed). ~41k USPS
    ZIP codes vs. ~33.8k that actually have their own ZCTA polygon — the
    remaining ~7k (PO boxes, unique-recipient, sparse/rural ZIPs) are mapped
    here to the nearest ZCTA that DOES have a polygon, so a ZIP lookup that
    misses the zcta table can still render an approximate boundary instead
    of coming back empty.
    """
    if not os.path.exists(ZIP_CROSSWALK_CSV_PATH):
        print("  ⚠️ zip_zcta_crosswalk.csv не найден — fallback для ZIP без своей ZCTA пропущен")
        return
    with open(ZIP_CROSSWALK_CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    conn.execute("DROP TABLE IF EXISTS zip_crosswalk")
    conn.execute("""CREATE TABLE zip_crosswalk (
        zip TEXT PRIMARY KEY, zcta TEXT, po_name TEXT, state TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zip_crosswalk_zcta ON zip_crosswalk(zcta)")

    batch = []
    for row in rows:
        zip_ = row["ZIP_CODE"].strip().zfill(5)
        zcta = (row.get("zcta") or "").strip()
        if not zcta:
            continue  # a handful of territory ZIPs have no ZCTA at all — can't fall back
        batch.append((zip_, zcta.zfill(5), row.get("PO_NAME", "").strip(), row.get("STATE", "").strip().upper()))
    conn.executemany("INSERT OR REPLACE INTO zip_crosswalk VALUES (?,?,?,?)", batch)
    conn.commit()
    print(f"  ✓ ZIP→ZCTA crosswalk: {len(batch)} ZIP-кодов")


def build_dma_counties(conn):
    """Nielsen DMA® (TV media market) → county crosswalk, bundled in the repo
    (no download needed). Most DMAs are whole-county aggregates, so the map
    can render a DMA boundary as the union of its member counties.
    """
    if not os.path.exists(DMA_CSV_PATH):
        print("  ⚠️ nielsen_dma_counties.csv не найден — DMA пропущены")
        return
    with open(DMA_CSV_PATH, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    conn.execute("DROP TABLE IF EXISTS dma_counties")
    conn.execute("""CREATE TABLE dma_counties (
        dma_label TEXT, county_name TEXT, state_abbr TEXT
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dma_label ON dma_counties(dma_label)")

    batch = []
    for row in rows:
        label = re.sub(r"\s*DMA\s*$", "", row["tvdma"].strip(), flags=re.I).strip()
        batch.append((label, row["county"].strip(), row["state_ab"].strip().upper()))
    conn.executemany("INSERT INTO dma_counties VALUES (?,?,?)", batch)
    conn.commit()
    n_labels = len({b[0] for b in batch})
    print(f"  ✓ DMA: {n_labels} рынков ({len(batch)} округов)")


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
