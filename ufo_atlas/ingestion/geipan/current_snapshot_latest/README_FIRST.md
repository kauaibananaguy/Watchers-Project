# GEIPAN official source snapshot — v0.2.0

Status: **PASS**  
Source collection: `SRC-COLLECTION-GEIPAN`  
Acquired: `2026-09-04T09:00:53Z`

This checkpoint combines GEIPAN's current live XLSX exports with its older, wider CSV exports. Every physical row is preserved as a separate source record. Overlapping current and legacy rows are reconciled but never collapsed or discarded.

## Source rows preserved

- Current case export: **3,381**
- Current testimony export: **6,068**
- Legacy detailed case export: **2,768**
- Legacy detailed testimony/observation export: **5,356**
- Total uninterrupted source ledger: **17,573**

## Reconciliation

- Unique case IDs across both case exports: **3,392**
- Present in both: **2,757**
- Current only: **624**
- Legacy only: **11**
- Unique normalized testimony-title keys: **6,176**

The current case XLSX count matches the current GEIPAN published-case count of **3,381** reported on the official statistics page dated **11/08/2026**.

## Architectural boundary

This is a source-preservation module for the one source-neutral UFO Atlas, not a public GEIPAN database. Canonical candidates, complete chronological narratives, GMR values, typed relationships, and final master-match proposals are built in the next stage.
