# Single-case ingestion

This directory provides a bounded path for adding one CE3+ case without rebuilding or editing an existing database in place.

The workflow has two deliberately separate stages:

1. Build a one-source, one-case GMR 1.0.0 import package. The package uses a `LOCAL-…` candidate ID and preserves the source account, normalized GMR values, source-field mapping, narrative, reconstruction, presentation metadata, and validation receipt.
2. After the latest master has been searched for duplicates and has assigned or confirmed a permanent `UFO-…` ID, copy the CE3+ presentation module and insert the related presentation rows in one transaction.

The package is a technical staging artifact, not a master release. The tool never chooses the next numeric ID, never edits its input database, and never promotes a package directly to production.

## Safety contract

- Copy-on-write only; source and output paths must differ.
- Optional SHA-256 pin for the source database.
- Exact target-schema and CE3+ module-role checks.
- Package logical fingerprint and embedded-asset hash verification.
- Permanent-ID gate: `apply-ce3plus` requires an externally assigned or confirmed `UFO-…` ID.
- `BEGIN IMMEDIATE` transaction for every related row.
- Duplicate behavior: an identical replay is a no-op; a changed replay is a hard conflict.
- Exact per-table row-delta assertions.
- Foreign-key and SQLite integrity checks before publication of the successor copy.
- Any error removes the temporary successor; the source remains untouched.

## Build a case package

From the repository root:

```bash
python ufo_atlas/ingestion/single_case/single_case.py build \
  --spec /private-staging/case.case.json \
  --gmr /path/to/UFO_ATLAS_GMR_v1.0.0.json \
  --output /staging/CLEAR_LAKE_1989_SINGLE_CASE_PACKAGE.sqlite

python ufo_atlas/ingestion/single_case/single_case.py validate \
  --package /staging/CLEAR_LAKE_1989_SINGLE_CASE_PACKAGE.sqlite
```

The current master-integration session must then:

1. freeze the current master;
2. search the complete master for the source ID, event date, Clear Lake/Soda Bay Road location, narrative fingerprints, and likely duplicates;
3. resolve the candidate as a new case, enrichment, source variant, or possible duplicate;
4. assign or confirm the permanent canonical ID;
5. import the GMR package into the master and rebuild only affected search rows;
6. run full master regression checks.

Only after that gate should the CE3+ presentation copy be created:

```bash
python ufo_atlas/ingestion/single_case/single_case.py apply-ce3plus \
  --package /staging/CLEAR_LAKE_1989_SINGLE_CASE_PACKAGE.sqlite \
  --source-db /verified/UFO_ATLAS_CE3PLUS_MODULE.sqlite \
  --expected-source-sha256 EXPECTED_64_HEX_DIGEST \
  --canonical-id UFO-ID-ASSIGNED-BY-MASTER \
  --output /staging/UFO_ATLAS_CE3PLUS_MODULE.successor.sqlite
```

Do not use a placeholder or guessed canonical ID in a release artifact.

## Tests

The tests use a disposable schema-compatible database and cover successful insertion, identical replay, changed replay, hash mismatch, and injected rollback:

```bash
python -m unittest discover \
  -s ufo_atlas/ingestion/single_case/tests \
  -p 'test_*.py' -v
```

Case specifications and media may contain sensitive witness information. Keep them in private staging or the controlled Atlas artifact store; do not commit them to a public repository. A case plate must be a 1920×1080 PNG or JPEG illustrative reconstruction, not event photography, and must retain its reconstruction label.
