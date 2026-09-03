#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PARTS="$ROOT/builder_parts/build_ufo_atlas_blue_book_import.py.gz.b64.part*"
cat $PARTS | tr -d '\r\n' > /tmp/build_ufo_atlas_blue_book_import.py.gz.b64
base64 --decode /tmp/build_ufo_atlas_blue_book_import.py.gz.b64 | gunzip > /tmp/build_ufo_atlas_blue_book_import.py
echo '121cdcf3f46cd883ec5a969cac26097a4bbcc96eee08c40060797cbe186ef05b  /tmp/build_ufo_atlas_blue_book_import.py' | sha256sum --check --strict
python -m py_compile /tmp/build_ufo_atlas_blue_book_import.py
