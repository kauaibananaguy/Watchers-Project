# Project Blue Book central-ingestion builder payload

These ordered text fragments reconstruct the validated Python ingestion builder used by the UFO Atlas workflow. They are transport fragments only; they are not a separate database or source-owned Atlas module.

Reconstruction:

```bash
cat build_ufo_atlas_blue_book_import.py.gz.b64.part* > /tmp/builder.py.gz.b64
base64 --decode /tmp/builder.py.gz.b64 | gunzip > /tmp/build_ufo_atlas_blue_book_import.py
sha256sum /tmp/build_ufo_atlas_blue_book_import.py
```

Expected builder SHA-256:

`121cdcf3f46cd883ec5a969cac26097a4bbcc96eee08c40060797cbe186ef05b`

The builder creates source-neutral `Case/Incident`, `Document`, `Location`, and attributed `Claim` records; leaves canonical Atlas IDs unallocated for central cross-source matching; preserves raw source transcription and provenance; creates no `Investigation/Project` record; and validates foreign keys, source parity, metadata decision parity, search parity, duplicate source files, and required case fields.
