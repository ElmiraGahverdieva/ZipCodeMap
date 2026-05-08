#!/usr/bin/env python3
"""
One-time setup: download US ZCTA shapefile from Census Bureau (~22 MB),
convert to GeoJSON, index into local SQLite database (~80 MB).
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

# Census Bureau cartographic boundary shapefiles (ZCTA, 1:500k simplified).
# Shapefiles exist for all these years; 2020 is tried first.
SOURCES = [
    "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip",
    "https://www2.census.gov/geo/tiger/GENZ2021/shp/cb_2021_us_zcta520_500k.zip",
    "https://www2.census.gov/geo/tiger/GENZ2022/shp/cb_2022_us_zcta520_500k.zip",
    "https://www2.census.gov/geo/tiger/GENZ2019/shp/cb_2019_us_zcta510_500k.zip",
    "https://www2.census.gov/geo/tiger/GENZ2018/shp/cb_2018_us_zcta510_500k.zip",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.census.gov/",
}


# ── Dependencies ──────────────────────────────────────────────────────────────

def ensure_pyshp():
    """Install pyshp (pure-Python shapefile reader) if not present."""
    try:
        import shapefile  # noqa: F401
        return True
    except ImportError:
        pass
    print("  Устанавливаю pyshp (чтение .shp без C-зависимостей)...")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "pyshp"],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print("  ✓ pyshp установлен")
        return True
    # Try pip3 as fallback
    result2 = subprocess.run(
        ["pip3", "install", "pyshp"],
        capture_output=True, text=True
    )
    if result2.returncode == 0:
        print("  ✓ pyshp установлен")
        return True
    print(f"  ❌ Не удалось установить pyshp:\n{result.stderr.strip()}")
    return False


# ── Download ──────────────────────────────────────────────────────────────────

def download(url):
    print(f"  URL: {url}")
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
                        mb = downloaded / 1024 / 1024
                        print(f"\r  [{bar}] {pct}%  {mb:.1f} MB", end="", flush=True)
        print(f"\r  {'█'*25} 100%  {downloaded/1024/1024:.1f} MB загружено")
        return tmp
    except Exception as e:
        print(f"\n  Ошибка: {e}")
        try:
            os.remove(tmp)
        except Exception:
            pass
        return None


# ── Convert ───────────────────────────────────────────────────────────────────

def shapefile_to_features(zip_path):
    import shapefile

    with tempfile.TemporaryDirectory() as tmpdir:
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmpdir)

        shp_files = [f for f in os.listdir(tmpdir) if f.lower().endswith(".shp")]
        if not shp_files:
            raise RuntimeError("Файл .shp не найден в архиве")

        sf = shapefile.Reader(os.path.join(tmpdir, shp_files[0]))
        fields = [f[0] for f in sf.fields[1:]]  # skip deletion flag

        zip_field = next(
            (c for c in ("ZCTA5CE20", "ZCTA5CE10", "ZCTA5", "ZIP") if c in fields),
            None,
        )
        if zip_field is None:
            raise RuntimeError(f"Поле ZIP не найдено. Доступные поля: {fields}")

        total = len(sf)
        print(f"  Поле ZIP: {zip_field}  |  объектов: {total}")

        features = []
        for i, sr in enumerate(sf.shapeRecords(), 1):
            if i % 5000 == 0:
                print(f"\r  Обработано: {i}/{total}", end="", flush=True)
            record = dict(zip(fields, sr.record))
            zip_code = str(record[zip_field]).strip().zfill(5)
            try:
                geometry = sr.shape.__geo_interface__
            except Exception:
                continue
            features.append({
                "type": "Feature",
                "properties": {"ZCTA5CE20": zip_code},
                "geometry": geometry,
            })

        if total > 5000:
            print()
        return features


# ── Index ─────────────────────────────────────────────────────────────────────

def build_db(features):
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("CREATE TABLE zcta (zip TEXT PRIMARY KEY, geometry TEXT)")
    conn.execute("CREATE INDEX idx_zip ON zcta(zip)")

    batch = []
    for feat in features:
        zip_code = feat["properties"]["ZCTA5CE20"]
        geom = json.dumps(feat["geometry"], separators=(",", ":"))
        batch.append((zip_code, geom))
        if len(batch) >= 2000:
            conn.executemany("INSERT OR REPLACE INTO zcta VALUES (?,?)", batch)
            batch.clear()

    if batch:
        conn.executemany("INSERT OR REPLACE INTO zcta VALUES (?,?)", batch)

    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM zcta").fetchone()[0]
    conn.close()
    return count


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # python3 setup.py --file path/to/cb_xxxx.zip
    manual_file = None
    if "--file" in sys.argv:
        idx = sys.argv.index("--file")
        if idx + 1 < len(sys.argv):
            manual_file = sys.argv[idx + 1]

    if os.path.exists(DB_PATH) and not manual_file:
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute("SELECT COUNT(*) FROM zcta").fetchone()[0]
        conn.close()
        print(f"✅ База уже создана: {n} ZIP-кодов ({os.path.basename(DB_PATH)})")
        print("   Для пересоздания: rm zcta.db && python3 setup.py")
        return True

    print("=== ZIP Code Map — первоначальная настройка ===\n")

    if not ensure_pyshp():
        print("\n❌ Установите вручную: pip3 install pyshp")
        return False

    tmp_path = None
    if manual_file:
        if not os.path.exists(manual_file):
            print(f"❌ Файл не найден: {manual_file}")
            return False
        print(f"  Использую: {manual_file}")
        tmp_path = manual_file
    else:
        for url in SOURCES:
            tmp_path = download(url)
            if tmp_path:
                break

    if not tmp_path:
        print("\n❌ Автозагрузка не удалась. Скачайте файл вручную:\n")
        print("  1. Откройте в браузере Safari или Chrome:")
        print("     https://www2.census.gov/geo/tiger/GENZ2020/shp/")
        print("  2. Кликните на: cb_2020_us_zcta520_500k.zip  (~22 MB)")
        print("  3. Переместите скачанный файл в папку zipcodemap/:")
        print("     mv ~/Downloads/cb_2020_us_zcta520_500k.zip ~/zipcodemap/")
        print("  4. Запустите:")
        print("     python3 setup.py --file cb_2020_us_zcta520_500k.zip")
        return False

    try:
        print("  Читаю shapefile...")
        features = shapefile_to_features(tmp_path)
        print(f"  Получено объектов: {len(features)}")

        print("  Создаю базу данных...")
        count = build_db(features)

        db_mb = os.path.getsize(DB_PATH) / 1024 / 1024
        print(f"\n✅ Готово!  {count} ZIP-кодов  →  {db_mb:.0f} MB  ({DB_PATH})")
        return True

    except Exception as e:
        print(f"❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
        return False

    finally:
        if not manual_file and tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
