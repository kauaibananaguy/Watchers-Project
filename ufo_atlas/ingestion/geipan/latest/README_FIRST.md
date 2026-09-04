# GEIPAN official source acquisition — v0.1.0

Status: **PASS**  
Source collection: `SRC-COLLECTION-GEIPAN`  
Acquired: `2026-09-04T08:39:01Z`

This checkpoint preserves the official GEIPAN case CSV, testimony/observation CSV, field-description workbook, database-history PDF, source page, and statistics page. It creates one uninterrupted immutable source ledger covering every nonblank row in both official CSV exports.

## Actual source volume

- Case rows: **2,768**
- Testimony/observation rows: **5,356**
- Total physical source rows: **8,124**
- Source files: **6**

## Architectural boundary

This is a source-acquisition checkpoint for the one source-neutral UFO Atlas. It is not a separate public GEIPAN database. No canonical candidates or irreversible matches have yet been created.

## Next stage

Map every GEIPAN source field to the controlling UFO Atlas GMR, preserve French source wording, construct source-neutral case/testimony/observation candidates, write English editorial translations separately, create typed relationships, and prepare duplicate proposals against the latest verified master.

## Live-site reconciliation

The official static case CSV contains **2,768** case rows. The live statistics page reports **3,381** published cases as of **11/08/2026**, leaving **613** published cases to acquire from the current site.

The static export is therefore a preserved source snapshot, not the final GEIPAN corpus.

## Official schema-workbook condition

The downloaded 2019 XLSX is retained byte-for-byte, but its ZIP component offsets are malformed and openpyxl cannot parse it. This does not affect preservation of the two CSV exports. All 35 case columns and 263 testimony/observation columns are preserved directly from their headers.
