#!/usr/bin/env python3
"""
One-time setup: download US ZCTA boundary GeoJSON from Census Bureau
and index it into a local SQLite database (~80 MB).
Run once, then start server.py.
"""
import io
import json
import os
import sqlite3
import sys
import urllib.request
import zipfile

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zcta.db")

# US Census Bureau cartographic boundary file (2020 ZCTA, 1:500k simplified)
SOURCES = [
    "https://www2.census.gov/geo/tiger/GENZ2020/geojson/cb_2020_us_zcta520_500k.zip",
    "https://www2.census.gov/geo/tiger/GENZ2022/geojson/cb_2022_us_zcta520_500k.zip",
]


def progress_hook(count, block_size, total_size):
    if total_size > 0:
        pct = min(int(count * block_size * 100 / total_size), 100)
        bar = "#" * (pct // 2) + "-" * (50 - pct // 2)
        print(f"\r  [{bar}] {pct}%", end="", flush=True)


def download(url):
    print(f"  Загрузка: {url}")
    try:
        path, _ = urllib.request.urlretrieve(url, reporthook=progress_hook)
        print()  # newline after progress bar
        return path
    except Exception as e:
        print(f"\n  Ошибка: {e}")
        return None


def find_geojson_in_zip(zip_path):
    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".json")]
        if not names:
            raise RuntimeError("GeoJSON не найден внутри архива")
        print(f"  Извлекаю: {names[0]}")
        return zf.read(names[0])


def build_db(features):
    print(f"  Создаю базу данных: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS zcta")
    conn.execute("CREATE TABLE zcta (zip TEXT PRIMARY KEY, geometry TEXT)")
    conn.execute("CREATE INDEX idx_zip ON zcta(zip)")

    batch = []
    skipped = 0
    for feat in features:
        props = feat.get("properties", {})
        zip_code = (
            props.get("ZCTA5CE20")
            or props.get("ZCTA5CE10")
            or props.get("ZCTA5")
            or props.get("ZIP")
        )
        if not zip_code:
            skipped += 1
            continue
        geom = json.dumps(feat["geometry"], separators=(",", ":"))
        batch.append((str(zip_code).zfill(5), geom))

        if len(batch) >= 2000:
            conn.executemany("INSERT OR REPLACE INTO zcta VALUES (?,?)", batch)
            batch.clear()

    if batch:
        conn.executemany("INSERT OR REPLACE INTO zcta VALUES (?,?)", batch)

    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM zcta").fetchone()[0]
    conn.close()
    return count, skipped


def main():
    if os.path.exists(DB_PATH):
        conn = sqlite3.connect(DB_PATH)
        n = conn.execute("SELECT COUNT(*) FROM zcta").fetchone()[0]
        conn.close()
        print(f"База данных уже существует: {n} ZIP-кодов в {DB_PATH}")
        print("Для пересоздания удалите файл zcta.db и запустите снова.")
        return True

    print("=== Загрузка данных ZIP-зон (US Census Bureau) ===\n")

    tmp_path = None
    raw_bytes = None

    for url in SOURCES:
        tmp_path = download(url)
        if tmp_path:
            break

    if not tmp_path:
        print("\n❌ Не удалось скачать данные. Проверьте интернет-соединение.")
        return False

    try:
        print("  Читаю архив...")
        raw_bytes = find_geojson_in_zip(tmp_path)
    except Exception as e:
        print(f"❌ Ошибка архива: {e}")
        return False
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    print("  Парсю GeoJSON...")
    geojson = json.loads(raw_bytes)
    features = geojson.get("features", [])
    print(f"  Найдено объектов: {len(features)}")

    count, skipped = build_db(features)
    print(f"\n✅ Готово! Добавлено {count} ZIP-кодов. (пропущено: {skipped})")
    db_mb = os.path.getsize(DB_PATH) / 1024 / 1024
    print(f"   База данных: {db_mb:.1f} MB → {DB_PATH}")
    return True


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)
