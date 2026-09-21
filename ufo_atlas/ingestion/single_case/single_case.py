#!/usr/bin/env python3
"""Build and safely apply one Watchers UFO Atlas case package.

The package is the durable source/provenance handoff. Applying it to the CE3+
presentation module is optional and copy-on-write. The canonical ID must be
assigned or confirmed by a master-integration session before application.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
from typing import Any, Iterable


SCHEMA_VERSION = "1.0.0"
REQUIRED_TARGET_TABLES = {
    "case_dossier",
    "case_portrait",
    "case_video_link",
    "case_video_player",
    "media_asset",
    "module_change_log",
    "production_batch",
    "video_catalog",
    "wp_case_route",
    "wp_encounter_class_policy",
    "wp_module_meta",
}
REQUIRED_CASE_FIELDS = {
    "core.canonical_title",
    "core.entry_scope_code",
    "core.primary_language_code",
    "core.record_id",
    "core.record_subtype_code",
    "core.record_type_code",
    "core.slug",
    "description.one_line_summary",
    "description.short_summary",
    "core.created_at",
    "core.created_by",
    "core.editorial_status_code",
    "core.public_visibility_code",
    "core.record_version",
    "core.updated_at",
    "core.updated_by",
    "quality.authenticity_status_code",
    "quality.classification_completeness_code",
    "quality.disputed_flag",
    "quality.editorial_confidence_code",
    "quality.record_quality_code",
    "quality.resolution_status_code",
    "quality.retraction_flag",
    "quality.source_completeness_code",
    "case.audio_flag",
    "case.electromagnetic_effect_flag",
    "case.environmental_effect_flag",
    "case.event_scope_code",
    "case.event_type_codes",
    "case.government_involvement_flag",
    "case.investigation_status_code",
    "case.law_enforcement_involvement_flag",
    "case.military_involvement_flag",
    "case.nuclear_association_flag",
    "case.photo_video_flag",
    "case.physical_trace_flag",
    "case.physiological_effect_flag",
    "case.psychological_effect_flag",
    "case.radar_sensor_flag",
    "case.repeat_event_flag",
    "case.water_association_flag",
}


class CaseError(RuntimeError):
    pass


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def word_count(text: str) -> int:
    return len(re.findall(r"\b[\w’'-]+\b", text, re.UNICODE))


def image_dimensions(data: bytes) -> tuple[int, int, str]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return width, height, "image/png"
    if data.startswith(b"\xff\xd8"):
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            i += 2
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                continue
            if i + 2 > len(data):
                break
            seglen = struct.unpack(">H", data[i:i + 2])[0]
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if i + 7 > len(data):
                    break
                height, width = struct.unpack(">HH", data[i + 3:i + 7])
                return width, height, "image/jpeg"
            i += seglen
    raise CaseError("portrait must be a readable PNG or JPEG")


def connect_ro(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


def require_clean_database(con: sqlite3.Connection, label: str) -> None:
    quick = con.execute("PRAGMA quick_check").fetchone()[0]
    if quick != "ok":
        raise CaseError(f"{label} quick_check failed: {quick}")
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    if fk:
        raise CaseError(f"{label} has {len(fk)} foreign-key violation(s)")


def load_gmr(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, set[str]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    registry = raw.get("registry") or []
    if not registry or registry[0].get("registry_version") != "1.0.0":
        raise CaseError("the selected GMR is not controlling version 1.0.0")
    fields = {row["field_key"]: row for row in raw["fields"]}
    vocab: dict[str, set[str]] = {}
    for row in raw["vocabulary_values"]:
        if str(row.get("status", "")).upper() == "ACTIVE":
            vocab.setdefault(row["vocabulary_code"], set()).add(row["value_code"])
    return fields, vocab


def normalize_value_rows(spec: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for field_key, raw in spec["case"]["gmr_values"].items():
        values = raw if isinstance(raw, list) else [raw]
        for ordinal, value in enumerate(values, 1):
            item = value if isinstance(value, dict) else {"value": value}
            rows.append(
                {
                    "local_record_id": spec["case"]["local_record_id"],
                    "field_key": field_key,
                    "value_ordinal": ordinal,
                    "value_type": item.get("value_type") or infer_value_type(item.get("value")),
                    "normalized_value": item.get("value"),
                    "vocabulary_code": item.get("vocabulary_code"),
                    "value_code": item.get("value_code"),
                    "original_value_text": item.get("original_value_text"),
                    "assertion_status_code": item.get("assertion_status_code", "NORMALIZED"),
                    "source_record_id": item.get("source_record_id", spec["source_record"]["source_record_id"]),
                    "source_locator": item.get("source_locator", spec["source_record"]["source_locator"]),
                    "transformation_note": item.get("transformation_note"),
                }
            )
    return rows


def infer_value_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "decimal"
    if isinstance(value, (dict, list)):
        return "JSON"
    return "text"


def applies_to(field: dict[str, Any], record_type: str) -> bool:
    allowed = {v.strip() for v in str(field.get("applies_to", "")).split(",")}
    return "ALL" in allowed or record_type in allowed


def validate_spec(spec: dict[str, Any], gmr_path: Path, base_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    for key in ("package", "source_record", "case", "portrait", "presentation", "source_field_mapping"):
        if key not in spec:
            raise CaseError(f"missing top-level section: {key}")
    package = spec["package"]
    if package.get("gmr_version") != "1.0.0":
        raise CaseError("package.gmr_version must be 1.0.0")
    if package.get("source_record_count") != 1:
        raise CaseError("single-case packages must declare exactly one source record")
    if spec["source_record"].get("source_sequence") != 1:
        raise CaseError("the single source record must have source_sequence 1")
    case = spec["case"]
    if case.get("record_type_code") != "CASE_EVENT":
        raise CaseError("the primary candidate must be CASE_EVENT")
    if not str(case.get("local_record_id", "")).startswith("LOCAL-"):
        raise CaseError("candidate ID must be module-local (LOCAL-…); the master assigns the permanent UFO ID")
    fields, vocab = load_gmr(gmr_path)
    rows = normalize_value_rows(spec)
    present = {row["field_key"] for row in rows}
    missing = sorted(REQUIRED_CASE_FIELDS - present)
    if missing:
        raise CaseError("required GMR fields missing: " + ", ".join(missing))
    for row in rows:
        field = fields.get(row["field_key"])
        if not field:
            raise CaseError(f"unknown GMR field: {row['field_key']}")
        if not applies_to(field, "CASE_EVENT"):
            raise CaseError(f"{row['field_key']} does not apply to CASE_EVENT")
        controlled = field.get("controlled_vocabulary")
        supplied_code = row.get("value_code")
        supplied_value = row.get("normalized_value")
        if controlled and supplied_code is None and controlled not in {"language_code", "time_zone", "dynamic_by_vocabulary"}:
            supplied_code = supplied_value
            row["value_code"] = supplied_code
            row["vocabulary_code"] = controlled
        if controlled and controlled not in {"language_code", "time_zone", "dynamic_by_vocabulary"}:
            if supplied_code not in vocab.get(controlled, set()):
                raise CaseError(f"invalid {controlled} value for {row['field_key']}: {supplied_code!r}")
        if row["assertion_status_code"] not in vocab.get("assertion_status", set()):
            raise CaseError(f"invalid assertion status: {row['assertion_status_code']}")
    if next(r for r in rows if r["field_key"] == "core.record_id")["normalized_value"] != case["local_record_id"]:
        raise CaseError("core.record_id must equal case.local_record_id")
    if not case.get("narrative") or word_count(case["narrative"]) < 150:
        raise CaseError("case narrative must contain at least 150 words")
    portrait_path = (base_dir / spec["portrait"]["path"]).resolve()
    if not portrait_path.is_file():
        raise CaseError(f"portrait not found: {portrait_path}")
    portrait_bytes = portrait_path.read_bytes()
    width, height, mime = image_dimensions(portrait_bytes)
    if (width, height) != (1920, 1080):
        raise CaseError(f"portrait must be 1920x1080; found {width}x{height}")
    portrait_info = {
        "path": portrait_path,
        "bytes": portrait_bytes,
        "sha256": sha256_bytes(portrait_bytes),
        "width": width,
        "height": height,
        "mime": mime,
    }
    expected_hash = spec["portrait"].get("sha256")
    if expected_hash and expected_hash != portrait_info["sha256"]:
        raise CaseError("portrait SHA-256 does not match the case specification")
    mappings = spec["source_field_mapping"]
    if not mappings or any(m.get("mapping_status") not in {"MAPPED", "EXTENSION", "RAW_ONLY", "NOT_APPLICABLE"} for m in mappings):
        raise CaseError("every source field must have a valid mapping status")
    return rows, portrait_info


PACKAGE_DDL = """
CREATE TABLE IMPORT_PACKAGE(
  package_id TEXT PRIMARY KEY, module_version TEXT NOT NULL, source_collection_id TEXT NOT NULL,
  gmr_version TEXT NOT NULL, master_snapshot_version TEXT NOT NULL, source_range_start TEXT NOT NULL,
  source_range_end TEXT NOT NULL, source_record_count INTEGER NOT NULL CHECK(source_record_count=1),
  created_at TEXT NOT NULL, predecessor_package_id TEXT, notes TEXT
);
CREATE TABLE SOURCE_RECORD_LEDGER(
  source_collection_id TEXT NOT NULL, source_sequence INTEGER PRIMARY KEY CHECK(source_sequence=1),
  source_record_id TEXT NOT NULL UNIQUE, source_file_id TEXT, source_locator TEXT NOT NULL,
  original_date_text TEXT, original_time_text TEXT, original_location_text TEXT, title_raw TEXT,
  description_raw TEXT, reference_raw TEXT, source_attributes_raw TEXT, raw_payload TEXT,
  proposed_resolution_code TEXT NOT NULL, proposed_target_local_or_master_id TEXT NOT NULL,
  resolution_basis TEXT NOT NULL, notes TEXT
);
CREATE TABLE CANDIDATE_RECORDS(
  local_record_id TEXT PRIMARY KEY, record_type_code TEXT NOT NULL, entry_scope_code TEXT NOT NULL,
  canonical_title TEXT NOT NULL, display_title TEXT, one_line_summary TEXT NOT NULL,
  short_summary TEXT NOT NULL, full_description_or_narrative TEXT NOT NULL,
  primary_language_code TEXT NOT NULL, proposed_existing_record_id TEXT, match_confidence_code TEXT,
  editorial_status_code TEXT NOT NULL, public_visibility_code TEXT NOT NULL
);
CREATE TABLE GMR_VALUES(
  local_record_id TEXT NOT NULL REFERENCES CANDIDATE_RECORDS(local_record_id) ON DELETE CASCADE,
  field_key TEXT NOT NULL, value_ordinal INTEGER NOT NULL, value_type TEXT NOT NULL,
  normalized_value TEXT, vocabulary_code TEXT, value_code TEXT, original_value_text TEXT,
  assertion_status_code TEXT NOT NULL, source_record_id TEXT NOT NULL,
  source_locator TEXT NOT NULL, transformation_note TEXT,
  PRIMARY KEY(local_record_id,field_key,value_ordinal)
);
CREATE TABLE RELATIONSHIPS(
  local_relationship_id TEXT PRIMARY KEY, subject_local_or_master_id TEXT NOT NULL,
  relationship_type_code TEXT NOT NULL, object_local_or_master_id TEXT NOT NULL,
  assertion_status_code TEXT NOT NULL, source_record_id TEXT NOT NULL,
  source_locator TEXT NOT NULL, start_date_or_time TEXT, end_date_or_time TEXT, notes TEXT
);
CREATE TABLE DUPLICATE_CANDIDATES(
  local_record_id TEXT NOT NULL, candidate_master_record_id TEXT NOT NULL, match_score REAL,
  match_basis TEXT NOT NULL, proposed_disposition TEXT NOT NULL, notes TEXT,
  PRIMARY KEY(local_record_id,candidate_master_record_id)
);
CREATE TABLE SOURCE_FIELD_MAPPING(
  source_field TEXT PRIMARY KEY, gmr_field_key TEXT, mapping_status TEXT NOT NULL,
  transformation_rule TEXT NOT NULL, extension_key TEXT, notes TEXT
);
CREATE TABLE MEDIA_ASSETS(
  asset_local_id TEXT PRIMARY KEY, local_record_id TEXT NOT NULL REFERENCES CANDIDATE_RECORDS(local_record_id),
  media_role TEXT NOT NULL, file_name TEXT NOT NULL, mime_type TEXT NOT NULL,
  width_px INTEGER NOT NULL, height_px INTEGER NOT NULL, sha256 TEXT NOT NULL,
  caption TEXT NOT NULL, authenticity_status TEXT NOT NULL, rights_basis TEXT NOT NULL,
  asset_bytes BLOB NOT NULL
);
CREATE TABLE PACKAGE_PRESENTATION(key TEXT PRIMARY KEY,value_json TEXT NOT NULL);
CREATE TABLE VALIDATION_REPORT(check_name TEXT PRIMARY KEY,status TEXT NOT NULL,detail TEXT NOT NULL);
"""


def scalar_for_db(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (dict, list)):
        return canonical_json(value)
    return str(value)


def package_logical_fingerprint(con: sqlite3.Connection) -> str:
    """Hash all import content while representing the BLOB by its declared hash."""
    tables = (
        "IMPORT_PACKAGE", "SOURCE_RECORD_LEDGER", "CANDIDATE_RECORDS", "GMR_VALUES",
        "RELATIONSHIPS", "DUPLICATE_CANDIDATES", "SOURCE_FIELD_MAPPING", "PACKAGE_PRESENTATION",
    )
    payload: dict[str, Any] = {}
    for table in tables:
        columns = [row[1] for row in con.execute(f"PRAGMA table_info({table})")]
        rows = [dict(zip(columns, row)) for row in con.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        payload[table] = rows
    media_columns = [row[1] for row in con.execute("PRAGMA table_info(MEDIA_ASSETS)") if row[1] != "asset_bytes"]
    payload["MEDIA_ASSETS"] = [
        dict(zip(media_columns, row))
        for row in con.execute(f"SELECT {','.join(media_columns)} FROM MEDIA_ASSETS ORDER BY rowid")
    ]
    return sha256_bytes(canonical_json(payload).encode("utf-8"))


def atomic_destination(path: Path, overwrite: bool) -> tuple[Path, Path]:
    path = path.resolve()
    if path.exists() and not overwrite:
        raise CaseError(f"destination exists (use --overwrite): {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    os.close(fd)
    temp = Path(name)
    temp.unlink()
    return path, temp


def build_package(spec_path: Path, gmr_path: Path, output: Path, overwrite: bool) -> dict[str, Any]:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    value_rows, portrait = validate_spec(spec, gmr_path, spec_path.parent)
    destination, temp = atomic_destination(output, overwrite)
    try:
        con = sqlite3.connect(temp)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.executescript(PACKAGE_DDL)
        p = spec["package"]
        con.execute(
            "INSERT INTO IMPORT_PACKAGE VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                p["package_id"], p["module_version"], p["source_collection_id"], p["gmr_version"],
                p["master_snapshot_version"], p["source_range_start"], p["source_range_end"],
                p["source_record_count"], p["created_at"], p.get("predecessor_package_id"), p.get("notes"),
            ),
        )
        s = spec["source_record"]
        source_cols = [r[1] for r in con.execute("PRAGMA table_info(SOURCE_RECORD_LEDGER)")]
        con.execute(
            f"INSERT INTO SOURCE_RECORD_LEDGER({','.join(source_cols)}) VALUES({','.join('?' for _ in source_cols)})",
            tuple(scalar_for_db(s.get(c)) for c in source_cols),
        )
        c = spec["case"]
        lookup = c["gmr_values"]
        def value(key: str) -> Any:
            raw = lookup[key]
            raw = raw[0] if isinstance(raw, list) else raw
            return raw.get("value") if isinstance(raw, dict) else raw
        con.execute(
            "INSERT INTO CANDIDATE_RECORDS VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                c["local_record_id"], c["record_type_code"], value("core.entry_scope_code"),
                value("core.canonical_title"), value("core.display_title") if "core.display_title" in lookup else None,
                value("description.one_line_summary"), value("description.short_summary"), c["narrative"],
                value("core.primary_language_code"), c.get("proposed_existing_record_id"), c.get("match_confidence_code"),
                value("core.editorial_status_code"), value("core.public_visibility_code"),
            ),
        )
        for row in value_rows:
            con.execute(
                "INSERT INTO GMR_VALUES VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["local_record_id"], row["field_key"], row["value_ordinal"], row["value_type"],
                    scalar_for_db(row["normalized_value"]), row["vocabulary_code"], row["value_code"],
                    row["original_value_text"], row["assertion_status_code"], row["source_record_id"],
                    row["source_locator"], row["transformation_note"],
                ),
            )
        for row in spec.get("relationships", []):
            con.execute("INSERT INTO RELATIONSHIPS VALUES(?,?,?,?,?,?,?,?,?,?)", tuple(row.get(k) for k in (
                "local_relationship_id", "subject_local_or_master_id", "relationship_type_code",
                "object_local_or_master_id", "assertion_status_code", "source_record_id", "source_locator",
                "start_date_or_time", "end_date_or_time", "notes")))
        for row in spec.get("duplicate_candidates", []):
            con.execute("INSERT INTO DUPLICATE_CANDIDATES VALUES(?,?,?,?,?,?)", tuple(row.get(k) for k in (
                "local_record_id", "candidate_master_record_id", "match_score", "match_basis",
                "proposed_disposition", "notes")))
        for row in spec["source_field_mapping"]:
            con.execute("INSERT INTO SOURCE_FIELD_MAPPING VALUES(?,?,?,?,?,?)", tuple(row.get(k) for k in (
                "source_field", "gmr_field_key", "mapping_status", "transformation_rule", "extension_key", "notes")))
        portrait_spec = spec["portrait"]
        con.execute(
            "INSERT INTO MEDIA_ASSETS VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                portrait_spec["asset_local_id"], c["local_record_id"], "PRIMARY_CASE_PORTRAIT",
                portrait["path"].name, portrait["mime"], portrait["width"], portrait["height"],
                portrait["sha256"], portrait_spec["caption"], "ILLUSTRATIVE_RECONSTRUCTION",
                portrait_spec["rights_basis"], portrait["bytes"],
            ),
        )
        for key, item in spec["presentation"].items():
            con.execute("INSERT INTO PACKAGE_PRESENTATION VALUES(?,?)", (key, canonical_json(item)))
        logical_fingerprint = package_logical_fingerprint(con)
        checks = {
            "gmr_conformance": "all supplied fields and controlled values resolved against GMR 1.0.0",
            "single_source_census": "1 physical source record; source sequence is continuous",
            "single_case_census": "1 CASE_EVENT candidate",
            "portrait_integrity": f"1920x1080 {portrait['mime']} sha256={portrait['sha256']}",
            "canonical_identity_gate": "permanent UFO ID intentionally unassigned pending latest-master duplicate check",
            "foreign_keys": "0 violations",
            "logical_fingerprint": logical_fingerprint,
        }
        for name, detail in checks.items():
            con.execute("INSERT INTO VALIDATION_REPORT VALUES(?,?,?)", (name, "PASS", detail))
        con.commit()
        require_clean_database(con, "new package")
        con.close()
        os.replace(temp, destination)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
        raise
    return {
        "status": "PASS",
        "operation": "build-package",
        "package": str(destination),
        "package_sha256": sha256_file(destination),
        "package_id": spec["package"]["package_id"],
        "case_local_id": spec["case"]["local_record_id"],
        "canonical_id_state": "AWAITING_MASTER_ASSIGNMENT_OR_CONFIRMATION",
    }


def package_rows(package_path: Path) -> tuple[sqlite3.Connection, sqlite3.Row, sqlite3.Row, dict[str, list[str]], sqlite3.Row, dict[str, Any]]:
    con = connect_ro(package_path)
    require_clean_database(con, "case package")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"IMPORT_PACKAGE", "SOURCE_RECORD_LEDGER", "CANDIDATE_RECORDS", "GMR_VALUES", "MEDIA_ASSETS", "PACKAGE_PRESENTATION", "VALIDATION_REPORT"}
    if missing := required - tables:
        raise CaseError("invalid package; missing tables: " + ", ".join(sorted(missing)))
    if con.execute("SELECT count(*) FROM IMPORT_PACKAGE").fetchone()[0] != 1:
        raise CaseError("package must contain one IMPORT_PACKAGE row")
    if con.execute("SELECT count(*) FROM SOURCE_RECORD_LEDGER").fetchone()[0] != 1:
        raise CaseError("package must contain one source record")
    if con.execute("SELECT count(*) FROM CANDIDATE_RECORDS WHERE record_type_code='CASE_EVENT'").fetchone()[0] != 1:
        raise CaseError("package must contain one CASE_EVENT candidate")
    failed = con.execute("SELECT * FROM VALIDATION_REPORT WHERE status<>'PASS'").fetchall()
    if failed:
        raise CaseError("package validation report contains failure(s)")
    fingerprint_row = con.execute("SELECT detail FROM VALIDATION_REPORT WHERE check_name='logical_fingerprint' AND status='PASS'").fetchone()
    if not fingerprint_row or fingerprint_row["detail"] != package_logical_fingerprint(con):
        raise CaseError("package logical fingerprint mismatch")
    package = con.execute("SELECT * FROM IMPORT_PACKAGE").fetchone()
    candidate = con.execute("SELECT * FROM CANDIDATE_RECORDS WHERE record_type_code='CASE_EVENT'").fetchone()
    values: dict[str, list[str]] = {}
    for row in con.execute("SELECT field_key,coalesce(value_code,normalized_value) value FROM GMR_VALUES WHERE local_record_id=? ORDER BY field_key,value_ordinal", (candidate["local_record_id"],)):
        values.setdefault(row["field_key"], []).append(row["value"])
    media = con.execute("SELECT * FROM MEDIA_ASSETS WHERE local_record_id=? AND media_role='PRIMARY_CASE_PORTRAIT'", (candidate["local_record_id"],)).fetchone()
    if not media:
        raise CaseError("package has no primary case portrait")
    if sha256_bytes(media["asset_bytes"]) != media["sha256"]:
        raise CaseError("packaged portrait hash mismatch")
    presentation = {r["key"]: json.loads(r["value_json"]) for r in con.execute("SELECT * FROM PACKAGE_PRESENTATION")}
    return con, package, candidate, values, media, presentation


def one(values: dict[str, list[str]], key: str) -> str:
    rows = values.get(key, [])
    if len(rows) != 1:
        raise CaseError(f"expected one value for {key}; found {len(rows)}")
    return rows[0]


def encounter_class(values: dict[str, list[str]]) -> str:
    codes = set(values.get("case.encounter_class_codes", []))
    if "HYNEK_CE3" in codes:
        return "CE3"
    if "EXTENDED_CE4" in codes:
        return "CE4"
    raise CaseError("CE3+ application requires HYNEK_CE3 or EXTENDED_CE4 classification")


def copy_database(source: Path, temp: Path) -> None:
    src = connect_ro(source)
    try:
        require_clean_database(src, "source module")
        dst = sqlite3.connect(temp)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()


def apply_ce3plus(
    package_path: Path,
    source_db: Path,
    output: Path,
    canonical_id: str,
    expected_source_sha256: str | None,
    overwrite: bool,
    fail_after: str | None,
) -> dict[str, Any]:
    if not re.fullmatch(r"UFO-[A-Z0-9][A-Z0-9-]*", canonical_id):
        raise CaseError("--canonical-id must be a master-assigned or confirmed UFO-… identifier")
    if source_db.resolve() == output.resolve():
        raise CaseError("in-place updates are prohibited; choose a new --output path")
    observed_source_hash = sha256_file(source_db)
    if expected_source_sha256 and observed_source_hash != expected_source_sha256:
        raise CaseError("source database hash differs from --expected-source-sha256")
    pkg_con, package, candidate, values, media, presentation = package_rows(package_path)
    destination, temp = atomic_destination(output, overwrite)
    copy_database(source_db, temp)
    con = sqlite3.connect(temp)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if missing := REQUIRED_TARGET_TABLES - tables:
            raise CaseError("wrong CE3+ target schema; missing: " + ", ".join(sorted(missing)))
        meta = dict(con.execute("SELECT key,value FROM wp_module_meta"))
        if meta.get("module_role") != "CE3_CE4_CE5_RICH_CONTENT":
            raise CaseError("target is not a CE3+ rich-content module")
        require_clean_database(con, "copied target module")
        ce = encounter_class(values)
        title = candidate["canonical_title"]
        narrative = candidate["full_description_or_narrative"]
        portrait_id = f"CE3P-{canonical_id}-PORTRAIT-001"
        batch_id = f"CE3PLUS-SINGLE-{package['package_id']}"
        source_rows = [dict(r) for r in pkg_con.execute("SELECT * FROM SOURCE_RECORD_LEDGER ORDER BY source_sequence")]
        source_basis = [{
            "source_record_id": r["source_record_id"],
            "title": r["title_raw"],
            "url": r["source_locator"],
            "role": "PRIMARY_RECORDED_TESTIMONY",
        } for r in source_rows]
        expected = {
            "encounter_class": ce,
            "canonical_title": title,
            "narrative_text": narrative,
            "portrait_sha256": media["sha256"],
        }
        existing = con.execute("SELECT * FROM case_dossier WHERE case_record_id=?", (canonical_id,)).fetchone()
        if existing:
            existing_media = con.execute("SELECT sha256 FROM media_asset WHERE media_asset_id=?", (existing["portrait_asset_id"],)).fetchone()
            same = (
                existing["encounter_class"] == ce
                and existing["canonical_title"] == title
                and existing["narrative_text"] == narrative
                and existing_media
                and existing_media["sha256"] == media["sha256"]
            )
            if not same:
                raise CaseError(f"canonical ID {canonical_id} already exists with different content")
            con.close()
            pkg_con.close()
            os.replace(temp, destination)
            return {
                "status": "PASS",
                "operation": "apply-ce3plus",
                "result": "IDENTICAL_REPLAY_NOOP",
                "canonical_id": canonical_id,
                "output": str(destination),
                "output_sha256": sha256_file(destination),
            }
        now = utc_now()
        before = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in REQUIRED_TARGET_TABLES}
        con.execute("BEGIN IMMEDIATE")
        con.execute("INSERT INTO production_batch VALUES(?,?,?,?)", (batch_id, 1, "VALIDATED_SINGLE_CASE_CHECKPOINT", now))
        con.execute(
            "INSERT INTO case_dossier VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                canonical_id, ce, title, f"CE3P-NARR-{canonical_id}-V1", narrative, word_count(narrative), 1,
                "PASS_SOURCE_GROUNDED_SOURCE_LIMITS_EXPLICIT", canonical_json(source_basis),
                canonical_json(presentation["visual_fact_packet"]), portrait_id,
                "DRAFT_COMPLETE_PENDING_INDEPENDENT_QA", "SINGLE_CASE_IMPORT_PENDING_MASTER_RELEASE_QA",
                batch_id, now, now,
            ),
        )
        if fail_after == "dossier":
            raise CaseError("injected failure after dossier")
        media_values = (
                portrait_id, canonical_id, "PRIMARY_CASE_PORTRAIT", "WATCHERS_RECONSTRUCTION", "HOSTED_COPY",
                presentation["title_overlay_text"], media["caption"], None, None,
                "Watchers Project / OpenAI ImageGen", "Watchers Project", media["rights_basis"],
                "Watchers Project illustrative reconstruction", "HIGH", "ILLUSTRATIVE_RECONSTRUCTION",
                "APPROVED_RECONSTRUCTION", "SOURCE_LINK_CARD",
                f"media/case_portraits/{canonical_id}-case-portrait.jpg", media["sha256"], media["mime_type"],
                media["width_px"], media["height_px"], 0, now, now, "Codex single-case ingestion",
                "Permanent reconstruction label is rendered into the image.", media["asset_bytes"],
            )
        con.execute(
            f"INSERT INTO media_asset VALUES({','.join('?' for _ in media_values)})",
            media_values,
        )
        con.execute(
            "INSERT INTO case_portrait VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                canonical_id, portrait_id, "16:9", presentation["generation_prompt"],
                presentation.get("generator_name", "OpenAI ImageGen built-in"),
                presentation.get("generator_version"), presentation["permanent_label"],
                presentation["title_overlay_text"], "VISUAL_QA_PASS", presentation["qa_notes"],
                presentation.get("generated_at", now), now, "Codex single-case ingestion",
            ),
        )
        video = presentation.get("video")
        if video:
            vid = video["video_id"]
            video_values = (
                    vid, "YOUTUBE", f"https://www.youtube.com/watch?v={vid}",
                    f"https://www.youtube-nocookie.com/embed/{vid}", video["title"],
                    video.get("channel_title"), video.get("channel_handle"), video.get("publication_date"),
                    video.get("duration_seconds"), video.get("thumbnail_url"),
                    video.get("ownership_code", "THIRD_PARTY_SOURCE_MIRROR"),
                    video.get("provenance_status", "USER_SUPPLIED_PRIMARY_CALL_RECORDING"), now,
                )
            existing_video = con.execute("SELECT * FROM video_catalog WHERE video_id=?", (vid,)).fetchone()
            if existing_video:
                if existing_video["canonical_url"] != video_values[2]:
                    raise CaseError(f"video ID {vid} already exists with a conflicting canonical URL")
                video_catalog_delta = 0
            else:
                con.execute("INSERT INTO video_catalog VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", video_values)
                video_catalog_delta = 1
            con.execute(
                "INSERT INTO case_video_link VALUES(?,?,?,?,?,?,?,?)",
                (vid, canonical_id, "PRIMARY_SOURCE_AUDIO_RECORDING", "HIGH", None, None, 1, now),
            )
            player_state, primary_video, empty = "READY", vid, ""
        else:
            player_state, primary_video, empty = "AWAITING_VERIFIED_VIDEO", None, "No verified case video linked yet."
            video_catalog_delta = 0
        con.execute(
            "INSERT INTO case_video_player VALUES(?,?,?,?,?,?,?,?,?)",
            (canonical_id, 1, player_state, primary_video, 0, 1, 1, empty, now),
        )
        policy = con.execute("SELECT priority,marker_color,module_code FROM wp_encounter_class_policy WHERE encounter_class=?", (ce,)).fetchone()
        if not policy:
            raise CaseError(f"target has no routing policy for {ce}")
        con.execute("INSERT INTO wp_case_route VALUES(?,?,?,?,?)", (canonical_id, ce, policy["module_code"], policy["priority"], policy["marker_color"]))
        receipt = {
            "status": "created",
            "package_id": package["package_id"],
            "package_sha256": sha256_file(package_path),
            "expected": expected,
        }
        con.execute(
            "INSERT INTO module_change_log(case_record_id,change_type,target_table,target_key,before_value,after_value,evidence_basis,changed_at,changed_by) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                canonical_id, "INSERT_SINGLE_CASE_ATOMIC", "case_dossier", canonical_id, None,
                canonical_json(receipt), canonical_json(source_basis), now, "Codex single-case ingestion",
            ),
        )
        if fail_after == "media":
            raise CaseError("injected failure after media")
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        if fk:
            raise CaseError(f"post-insert foreign-key check found {len(fk)} violation(s)")
        after = {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in REQUIRED_TARGET_TABLES}
        expected_delta = {
            "case_dossier": 1, "case_portrait": 1, "case_video_player": 1, "media_asset": 1,
            "module_change_log": 1, "production_batch": 1, "wp_case_route": 1,
            "video_catalog": video_catalog_delta, "case_video_link": 1 if video else 0,
            "wp_encounter_class_policy": 0, "wp_module_meta": 0,
        }
        bad = {t: after[t] - before[t] for t in REQUIRED_TARGET_TABLES if after[t] - before[t] != expected_delta[t]}
        if bad:
            raise CaseError(f"unexpected table deltas: {bad}")
        con.commit()
        require_clean_database(con, "successor module")
        con.close()
        pkg_con.close()
        os.replace(temp, destination)
    except Exception:
        with contextlib.suppress(Exception):
            con.rollback()
        with contextlib.suppress(Exception):
            con.close()
        with contextlib.suppress(Exception):
            pkg_con.close()
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
        raise
    return {
        "status": "PASS",
        "operation": "apply-ce3plus",
        "result": "INSERTED_ONE_CASE",
        "canonical_id": canonical_id,
        "source_sha256": observed_source_hash,
        "output": str(destination),
        "output_sha256": sha256_file(destination),
        "row_deltas": expected_delta,
    }


def inspect_package(path: Path) -> dict[str, Any]:
    con, package, candidate, values, media, presentation = package_rows(path)
    result = {
        "status": "PASS",
        "operation": "validate-package",
        "package_id": package["package_id"],
        "package_sha256": sha256_file(path),
        "source_records": con.execute("SELECT count(*) FROM SOURCE_RECORD_LEDGER").fetchone()[0],
        "case_candidates": con.execute("SELECT count(*) FROM CANDIDATE_RECORDS WHERE record_type_code='CASE_EVENT'").fetchone()[0],
        "gmr_values": con.execute("SELECT count(*) FROM GMR_VALUES").fetchone()[0],
        "canonical_id_state": "AWAITING_MASTER_ASSIGNMENT_OR_CONFIRMATION",
        "portrait": {"sha256": media["sha256"], "width": media["width_px"], "height": media["height_px"]},
    }
    con.close()
    return result


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="build a validated one-case GMR package")
    b.add_argument("--spec", type=Path, required=True)
    b.add_argument("--gmr", type=Path, required=True)
    b.add_argument("--output", type=Path, required=True)
    b.add_argument("--overwrite", action="store_true")
    v = sub.add_parser("validate", help="validate an existing one-case package")
    v.add_argument("--package", type=Path, required=True)
    a = sub.add_parser("apply-ce3plus", help="copy a CE3+ module and atomically insert the case")
    a.add_argument("--package", type=Path, required=True)
    a.add_argument("--source-db", type=Path, required=True)
    a.add_argument("--output", type=Path, required=True)
    a.add_argument("--canonical-id", required=True)
    a.add_argument("--expected-source-sha256")
    a.add_argument("--overwrite", action="store_true")
    a.add_argument("--fail-after", choices=("dossier", "media"), help=argparse.SUPPRESS)
    return ap


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_package(args.spec, args.gmr, args.output, args.overwrite)
        elif args.command == "validate":
            result = inspect_package(args.package)
        else:
            result = apply_ce3plus(
                args.package, args.source_db, args.output, args.canonical_id,
                args.expected_source_sha256, args.overwrite, args.fail_after,
            )
        print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except (CaseError, json.JSONDecodeError, sqlite3.Error, OSError, KeyError) as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
