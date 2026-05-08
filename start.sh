#!/bin/bash
# ZIP Code Map — one-click launcher for macOS
set -e
cd "$(dirname "$0")"

if [ ! -f "zcta.db" ]; then
  echo "Первый запуск: скачиваю данные ZIP-зон (~25 MB)..."
  python3 setup.py || { echo "Ошибка загрузки данных. Проверьте интернет."; exit 1; }
fi

python3 server.py
