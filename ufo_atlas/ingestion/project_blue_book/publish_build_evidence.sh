#!/usr/bin/env bash
set -euo pipefail

LATEST='ufo_atlas/ingestion/project_blue_book/latest'
mkdir -p "$LATEST"
cp build/project_blue_book/MANIFEST.json "$LATEST/MANIFEST.json"
cp build/project_blue_book/VALIDATION_REPORT.json "$LATEST/VALIDATION_REPORT.json"
cp build/project_blue_book/README.md "$LATEST/README.md"
cp build/project_blue_book/SHA256SUMS.txt "$LATEST/SHA256SUMS.txt"
cp build/project_blue_book/SOURCE_REPOSITORY_COMMIT.txt "$LATEST/SOURCE_REPOSITORY_COMMIT.txt"
cp build/UFO_ATLAS_CENTRAL_IMPORT_PROJECT_BLUE_BOOK_v0.1.0.zip.sha256 "$LATEST/PACKAGE_SHA256.txt"
printf '%s\n' "${GITHUB_RUN_ID:-LOCAL}" > "$LATEST/WORKFLOW_RUN_ID.txt"

git config user.name 'github-actions[bot]'
git config user.email '41898282+github-actions[bot]@users.noreply.github.com'
git add "$LATEST"
if ! git diff --cached --quiet; then
  git commit -m 'Publish validated Project Blue Book central-import build evidence'
  git push origin HEAD:main
fi
