#!/usr/bin/env python3
"""
Diagnostic downloader — tries multiple sources and shows exactly what goes wrong.
Run: python3 download_data.py
"""
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

OUT = "cb_2020_us_zcta520_500k.zip"

URLS = [
    # Census Bureau — different paths
    "https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip",
    "https://www2.census.gov/geo/tiger/GENZ2021/shp/cb_2021_us_zcta520_500k.zip",
    "https://www2.census.gov/geo/tiger/TIGER2020/ZCTA5/tl_2020_us_zcta510.zip",
    # FTP mirror
    "https://www2.census.gov/geo/tiger/GENZ2019/shp/cb_2019_us_zcta510_500k.zip",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
    "Connection": "keep-alive",
    "Referer": "https://www.census.gov/cgi-bin/geo/shapefiles/index.php",
}

# Allow self-signed / older certs (some gov servers need this)
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def try_download(url):
    print(f"\n→ {url}")
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, context=ctx, timeout=120) as resp:
            ct = resp.headers.get("Content-Type", "?")
            cl = resp.headers.get("Content-Length", "?")
            print(f"  Status: {resp.status}  Content-Type: {ct}  Size: {cl}")

            if "html" in ct.lower():
                snippet = resp.read(400).decode("utf-8", errors="replace")
                print(f"  ❌ Сервер вернул HTML (ошибка):\n  {snippet[:200]}")
                return False

            total = int(cl) if cl.isdigit() else 0
            downloaded = 0
            with open(OUT, "wb") as f:
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

            mb = downloaded / 1024 / 1024
            print(f"\r  {'█'*25} 100%  {mb:.1f} MB скачано         ")

            if mb < 1:
                print("  ❌ Файл слишком маленький — скорее всего HTML с ошибкой")
                os.remove(OUT)
                return False

            print(f"  ✅ Сохранено: {OUT}")
            return True

    except urllib.error.HTTPError as e:
        print(f"  ❌ HTTP {e.code}: {e.reason}")
    except Exception as e:
        print(f"  ❌ {type(e).__name__}: {e}")
    return False


if __name__ == "__main__":
    # Remove corrupt file from previous attempt
    if os.path.exists(OUT):
        size = os.path.getsize(OUT)
        print(f"Удаляю предыдущий файл ({size/1024:.0f} KB)...")
        os.remove(OUT)

    print("=== Загрузка данных ZIP-зон ===")
    for url in URLS:
        if try_download(url):
            print("\n✅ Запускайте:")
            print(f"   python3 setup.py --file {OUT}")
            sys.exit(0)

    print("\n" + "="*50)
    print("❌ Все источники недоступны с вашей сети.")
    print()
    print("РУЧНОЙ СПОСОБ:")
    print("1. Откройте в браузере Safari:")
    print("   https://www2.census.gov/geo/tiger/GENZ2020/shp/cb_2020_us_zcta520_500k.zip")
    print("   (ссылку нужно скопировать и вставить в адресную строку — начнётся скачивание)")
    print()
    print("2. Найдите файл в Downloads:")
    print("   ls ~/Downloads/ | grep zcta")
    print()
    print("3. Переместите:")
    print("   mv ~/Downloads/cb_2020_us_zcta520_500k.zip ~/zipcodemap/")
    print()
    print("4. Запустите:")
    print("   python3 setup.py --file cb_2020_us_zcta520_500k.zip")
    sys.exit(1)
