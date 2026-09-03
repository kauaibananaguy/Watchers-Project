#!/usr/bin/env bash
set -euo pipefail
source /tmp/project_blue_book_source.env

rm -rf build/project_blue_book
mkdir -p build/project_blue_book
PYTHONUTF8=1 PYTHONHASHSEED=0 python /tmp/build_ufo_atlas_blue_book_import.py \
  --source-root /tmp/blue_book_scanner/data/scanned_casefiles \
  --output-dir build/project_blue_book \
  --source-commit "$SOURCE_COMMIT" \
  | tee build/project_blue_book/BUILD_RUN_REPORT.json

jq -e '
  .overall_status == "PASS" and
  .counts.source_files > 1000 and
  .counts.case_records == .counts.source_files and
  .counts.document_records == .counts.source_files and
  .no_project_records == true and
  .foreign_key_violations == 0 and
  .case_source_parity == true and
  .fts_parity == true and
  (.gates | all(. == true))
' build/project_blue_book/VALIDATION_REPORT.json

DB="$(find build/project_blue_book -maxdepth 1 -type f -name '*.sqlite' -print -quit)"
test -n "$DB"
python - "$DB" <<'PY'
import sqlite3, sys
con = sqlite3.connect(sys.argv[1])
assert con.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
assert con.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
assert con.execute('PRAGMA foreign_key_check').fetchall() == []
assert con.execute("SELECT count(*) FROM records WHERE record_type='Investigation/Project'").fetchone()[0] == 0
cases = con.execute("SELECT count(*) FROM records WHERE record_type='Case/Incident'").fetchone()[0]
docs = con.execute("SELECT count(*) FROM records WHERE record_type='Document'").fetchone()[0]
sources = con.execute('SELECT source_file_count FROM source_snapshots').fetchone()[0]
assert cases == docs == sources
con.close()
print(f'Acceptance gate verified for {cases:,} source cases.')
PY

printf '%s\n' "$SOURCE_COMMIT" > build/project_blue_book/SOURCE_REPOSITORY_COMMIT.txt

# The builder writes its checksum ledger before tee finishes BUILD_RUN_REPORT.json
# and before SOURCE_REPOSITORY_COMMIT.txt exists. Rebuild the ledger only after
# every package member is final, then verify it before creating the ZIP.
(
  cd build/project_blue_book
  checksum_tmp="$(mktemp)"
  find . -maxdepth 1 -type f ! -name 'SHA256SUMS.txt' -printf '%f\n' \
    | LC_ALL=C sort \
    | while IFS= read -r file; do sha256sum "$file"; done \
    > "$checksum_tmp"
  mv "$checksum_tmp" SHA256SUMS.txt
  sha256sum -c SHA256SUMS.txt
)

(
  cd build
  zip -q -r -9 UFO_ATLAS_CENTRAL_IMPORT_PROJECT_BLUE_BOOK_v0.1.0.zip project_blue_book
)
sha256sum build/UFO_ATLAS_CENTRAL_IMPORT_PROJECT_BLUE_BOOK_v0.1.0.zip > build/UFO_ATLAS_CENTRAL_IMPORT_PROJECT_BLUE_BOOK_v0.1.0.zip.sha256
