#!/usr/bin/env bash
set -euo pipefail

rm -rf /tmp/blue_book_scanner
git clone --depth 1 --filter=blob:none --sparse https://github.com/dansterdam/blue_book_scanner.git /tmp/blue_book_scanner
git -C /tmp/blue_book_scanner sparse-checkout set \
  data/scanned_casefiles/1940s_cases \
  data/scanned_casefiles/1950s_cases \
  data/scanned_casefiles/1960s_cases \
  data/scanned_casefiles/19XXs_cases

SOURCE_COMMIT="$(git -C /tmp/blue_book_scanner rev-parse HEAD)"
SOURCE_FILES="$(find /tmp/blue_book_scanner/data/scanned_casefiles -type f -name '*.txt' | wc -l | tr -d ' ')"
test "$SOURCE_FILES" -gt 1000
printf 'SOURCE_COMMIT=%s\nSOURCE_FILES=%s\n' "$SOURCE_COMMIT" "$SOURCE_FILES" > /tmp/project_blue_book_source.env
