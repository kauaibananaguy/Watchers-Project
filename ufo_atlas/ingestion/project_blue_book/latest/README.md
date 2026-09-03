# UFO Atlas central import — Project Blue Book source corpus

Build version: 0.1.0  
Schema profile: UFO-ATLAS-IMPORT-0.1  
Source repository commit: 53caa32810d60638b82f39e93bae501b4ba7a6a6  
Built: 2026-09-03T13:44:32Z

This package is a **source-neutral UFO Atlas import database**. It does not create a separate Project Blue Book project hierarchy. Each source file produces a `Case/Incident` entry, a linked `Document` entry, and a normalized `Location` entry where a place can be recovered. Project Blue Book is represented only in document source/provenance, external identifiers, and attributed source dispositions.

## Package counts

- Source case files: 10,807
- Case/Incident records: 10,807
- Document records: 10,807
- Distinct Location records: 6,805
- Claim records: 2,041
- Relationships: 23,655
- Explicit metadata decisions: 410,666
- Case events: 58,859
- Open ingestion issues: 7,739

## Central-merge behavior

`record_id` values are stable import keys. `atlas_record_id` remains unallocated so the central UFO Atlas merger can first match against NICAP and other sources. Each case carries a `date_location_match_key`, source IDs, source filename, raw source text hash, and merge action `CREATE_OR_MATCH`.

The database contains no `Investigation/Project` record. A case that already exists in the central Atlas should be enriched with the document, source metadata, terms, and chronology rather than duplicated.

## Narrative status

Narratives are conservative source-derived reconstructions. `PROVISIONAL_SOURCE_DERIVED` means usable event text was extracted from the transcription. `MINIMAL_PENDING_SOURCE_VERIFICATION` means only a bounded minimal narrative could be constructed. Original source text is retained in `documents.raw_transcription` for verification and later full chronological reconstruction.

## Source limitation

Third-party GPT-assisted transcription of scanned government case files. Accuracy is not guaranteed; values and narratives require comparison with the original scans.

Original scans should control whenever the transcription conflicts with them.

## Validation

Overall status: **PASS**  
SQLite quick check: `ok`  
Foreign-key violations: 0  
No Investigation/Project records: True  
Case/source parity: True  
FTS/search parity: True
