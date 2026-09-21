import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest
import zlib


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "single_case.py"
loader_spec = importlib.util.spec_from_file_location("single_case", MODULE_PATH)
single_case = importlib.util.module_from_spec(loader_spec)
assert loader_spec.loader
loader_spec.loader.exec_module(single_case)


TARGET_DDL = """
CREATE TABLE production_batch(batch_id TEXT PRIMARY KEY,case_count INTEGER NOT NULL,status TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE case_dossier(case_record_id TEXT PRIMARY KEY,encounter_class TEXT NOT NULL,canonical_title TEXT NOT NULL,narrative_id TEXT NOT NULL UNIQUE,narrative_text TEXT NOT NULL,word_count INTEGER NOT NULL,chronology_complete INTEGER NOT NULL CHECK(chronology_complete IN(0,1)),unsupported_detail_check TEXT NOT NULL,source_basis_json TEXT NOT NULL,visual_fact_packet_json TEXT NOT NULL,portrait_asset_id TEXT NOT NULL UNIQUE,editorial_status TEXT NOT NULL,production_status TEXT NOT NULL,batch_id TEXT NOT NULL REFERENCES production_batch(batch_id),created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE media_asset(media_asset_id TEXT PRIMARY KEY,case_record_id TEXT NOT NULL REFERENCES case_dossier(case_record_id) ON DELETE CASCADE,media_role TEXT NOT NULL,evidentiary_lane TEXT NOT NULL,delivery_mode TEXT NOT NULL,display_title TEXT NOT NULL,caption TEXT,source_page_url TEXT,direct_asset_url TEXT,creator_text TEXT,owner_text TEXT,license_basis TEXT,attribution_text TEXT,provenance_confidence TEXT NOT NULL,authenticity_status TEXT NOT NULL,review_status TEXT NOT NULL,fallback_behavior TEXT,local_relative_path TEXT,sha256 TEXT,mime_type TEXT,width_px INTEGER,height_px INTEGER,sort_order INTEGER NOT NULL DEFAULT 0,source_retrieved_at TEXT,reviewed_at TEXT,reviewer TEXT,notes TEXT,asset_bytes BLOB,CHECK((delivery_mode='HOSTED_COPY' AND asset_bytes IS NOT NULL AND sha256 IS NOT NULL) OR delivery_mode<>'HOSTED_COPY'));
CREATE TABLE case_portrait(case_record_id TEXT PRIMARY KEY REFERENCES case_dossier(case_record_id) ON DELETE CASCADE,media_asset_id TEXT NOT NULL UNIQUE REFERENCES media_asset(media_asset_id),aspect_ratio TEXT NOT NULL,generation_prompt TEXT NOT NULL,generator_name TEXT,generator_version TEXT,permanent_label TEXT NOT NULL,title_overlay_text TEXT NOT NULL,qa_status TEXT NOT NULL,qa_notes TEXT,generated_at TEXT,reviewed_at TEXT,reviewer TEXT);
CREATE TABLE video_catalog(video_id TEXT PRIMARY KEY,provider_code TEXT NOT NULL,canonical_url TEXT NOT NULL,embed_url TEXT NOT NULL,title TEXT NOT NULL,channel_title TEXT,channel_handle TEXT,publication_date TEXT,duration_seconds INTEGER,thumbnail_url TEXT,ownership_code TEXT,provenance_status TEXT,observed_at TEXT);
CREATE TABLE case_video_link(video_id TEXT NOT NULL REFERENCES video_catalog(video_id) ON DELETE CASCADE,case_record_id TEXT NOT NULL REFERENCES case_dossier(case_record_id) ON DELETE CASCADE,relationship_code TEXT NOT NULL,mapping_confidence TEXT NOT NULL,start_seconds INTEGER,end_seconds INTEGER,fullscreen_allowed INTEGER NOT NULL CHECK(fullscreen_allowed=1),reviewed_at TEXT,PRIMARY KEY(video_id,case_record_id));
CREATE TABLE case_video_player(case_record_id TEXT PRIMARY KEY REFERENCES case_dossier(case_record_id) ON DELETE CASCADE,player_enabled INTEGER NOT NULL CHECK(player_enabled=1),player_state TEXT NOT NULL CHECK(player_state IN('READY','AWAITING_VERIFIED_VIDEO')),primary_video_id TEXT REFERENCES video_catalog(video_id),autoplay INTEGER NOT NULL CHECK(autoplay=0),fullscreen_enabled INTEGER NOT NULL CHECK(fullscreen_enabled=1),privacy_enhanced_embed INTEGER NOT NULL CHECK(privacy_enhanced_embed=1),empty_state_text TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE module_change_log(change_id INTEGER PRIMARY KEY,case_record_id TEXT NOT NULL REFERENCES case_dossier(case_record_id) ON DELETE CASCADE,change_type TEXT NOT NULL,target_table TEXT,target_key TEXT,before_value TEXT,after_value TEXT,evidence_basis TEXT,changed_at TEXT,changed_by TEXT);
CREATE TABLE wp_module_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE wp_encounter_class_policy(encounter_class TEXT PRIMARY KEY,priority INTEGER NOT NULL,marker_color TEXT NOT NULL,marker_family TEXT,module_code TEXT NOT NULL,rich_content_priority INTEGER,label TEXT);
CREATE TABLE wp_case_route(case_record_id TEXT PRIMARY KEY,encounter_class TEXT NOT NULL,module_code TEXT NOT NULL,priority INTEGER NOT NULL,marker_color TEXT NOT NULL);
"""


def make_png_1920x1080(path):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    row = b"\x00" + (b"\x10\x20\x30" * 1920)
    raw = row * 1080
    data = b"\x89PNG\r\n\x1a\n"
    data += chunk(b"IHDR", struct.pack(">IIBBBBB", 1920, 1080, 8, 2, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(raw, 9))
    data += chunk(b"IEND", b"")
    path.write_bytes(data)
    return single_case.sha256_bytes(data)


def controlled(value, vocabulary, assertion="NORMALIZED"):
    return {"value": value, "value_code": value, "vocabulary_code": vocabulary, "assertion_status_code": assertion}


def make_fixture_spec(portrait_hash):
    now = "2026-01-01T00:00:00Z"
    case_id = "LOCAL-CASE-SYNTHETIC-001"
    values = {
        "core.record_id": {"value": case_id, "assertion_status_code": "NORMALIZED"},
        "core.record_type_code": controlled("CASE_EVENT", "record_type"),
        "core.record_subtype_code": controlled("CASE_ENTITY_ENCOUNTER", "record_subtype"),
        "core.entry_scope_code": controlled("SPECIFIC_INSTANCE", "entry_scope"),
        "core.canonical_title": {"value": "Synthetic test encounter", "assertion_status_code": "NORMALIZED"},
        "core.primary_language_code": {"value": "en", "assertion_status_code": "NORMALIZED"},
        "core.slug": {"value": "synthetic-test-encounter", "assertion_status_code": "NORMALIZED"},
        "description.one_line_summary": {"value": "A synthetic record used only to test atomic ingestion.", "assertion_status_code": "NORMALIZED"},
        "description.short_summary": {"value": "This is generated test data with no real witness, place, or event.", "assertion_status_code": "NORMALIZED"},
        "core.created_at": {"value": now, "assertion_status_code": "NORMALIZED"},
        "core.created_by": {"value": "unit test", "assertion_status_code": "NORMALIZED"},
        "core.editorial_status_code": controlled("PUBLISHED", "editorial_status"),
        "core.public_visibility_code": controlled("PUBLIC", "public_visibility"),
        "core.record_version": {"value": 1, "assertion_status_code": "NORMALIZED"},
        "core.updated_at": {"value": now, "assertion_status_code": "NORMALIZED"},
        "core.updated_by": {"value": "unit test", "assertion_status_code": "NORMALIZED"},
        "quality.authenticity_status_code": controlled("REPORTED_ONLY", "authenticity_status"),
        "quality.classification_completeness_code": controlled("SUBSTANTIAL", "completeness_level"),
        "quality.disputed_flag": {"value": False, "assertion_status_code": "NORMALIZED"},
        "quality.editorial_confidence_code": controlled("HIGH", "confidence_level"),
        "quality.record_quality_code": controlled("GOOD", "record_quality"),
        "quality.resolution_status_code": controlled("UNRESOLVED", "resolution_status"),
        "quality.retraction_flag": {"value": False, "assertion_status_code": "NORMALIZED"},
        "quality.source_completeness_code": controlled("PARTIAL", "completeness_level"),
        "case.event_scope_code": controlled("SINGLE_EVENT", "case_event_scope"),
        "case.event_type_codes": controlled("ENTITY_ENCOUNTER", "case_event_type"),
        "case.encounter_class_codes": controlled("HYNEK_CE3", "encounter_class"),
        "case.investigation_status_code": controlled("UNKNOWN", "investigation_status"),
    }
    for key in (
        "case.audio_flag", "case.electromagnetic_effect_flag", "case.environmental_effect_flag",
        "case.government_involvement_flag", "case.law_enforcement_involvement_flag",
        "case.military_involvement_flag", "case.nuclear_association_flag", "case.photo_video_flag",
        "case.physical_trace_flag", "case.physiological_effect_flag", "case.psychological_effect_flag",
        "case.radar_sensor_flag", "case.repeat_event_flag", "case.water_association_flag",
    ):
        values[key] = {"value": False, "assertion_status_code": "NORMALIZED"}
    narrative = " ".join([
        "This synthetic case exists only to exercise the single-case database transaction.",
        "It does not describe a real person, location, object, entity, injury, or historical event.",
        "The narrative is intentionally long enough to pass the editorial minimum enforced by the package builder.",
        "A fictional observer begins a fictional sequence, records a fictional observation, and reaches a clear ending.",
        "Every detail is test data and carries no factual or evidentiary claim.",
    ] * 7)
    return {
        "package": {"package_id": "SYNTHETIC-TEST-001", "module_version": "1.0.0", "source_collection_id": "SYNTHETIC", "gmr_version": "1.0.0", "master_snapshot_version": "TEST", "source_range_start": "1", "source_range_end": "1", "source_record_count": 1, "created_at": now, "notes": "Synthetic unit fixture"},
        "source_record": {"source_collection_id": "SYNTHETIC", "source_sequence": 1, "source_record_id": "SYNTHETIC-SOURCE-001", "source_file_id": None, "source_locator": "https://example.invalid/synthetic", "original_date_text": None, "original_time_text": None, "original_location_text": None, "title_raw": "Synthetic source", "description_raw": "Synthetic source used only by unit tests.", "reference_raw": None, "source_attributes_raw": None, "raw_payload": {"synthetic": True}, "proposed_resolution_code": "NEW_CANONICAL_CASE", "proposed_target_local_or_master_id": case_id, "resolution_basis": "Synthetic test", "notes": None},
        "case": {"local_record_id": case_id, "record_type_code": "CASE_EVENT", "proposed_existing_record_id": None, "match_confidence_code": None, "narrative": narrative, "gmr_values": values},
        "relationships": [], "duplicate_candidates": [],
        "portrait": {"asset_local_id": "LOCAL-MEDIA-SYNTHETIC-001", "path": "assets/synthetic.png", "sha256": portrait_hash, "caption": "Synthetic reconstruction used only for tests.", "rights_basis": "Generated test fixture"},
        "presentation": {"title_overlay_text": "SYNTHETIC TEST", "permanent_label": "ILLUSTRATIVE RECONSTRUCTION • TEST", "generation_prompt": "Synthetic test fixture", "qa_notes": "Generated 1920x1080 PNG", "visual_fact_packet": {"setting": "synthetic", "exclusions": "all real-world claims"}, "video": {"video_id": "Synthetic01", "title": "Synthetic test video"}},
        "source_field_mapping": [{"source_field": "synthetic", "gmr_field_key": "description.short_summary", "mapping_status": "MAPPED", "transformation_rule": "Synthetic fixture", "extension_key": None, "notes": None}],
    }


def make_minimal_gmr(spec, path):
    fields = []
    vocab_values = []
    seen_values = set()
    for key, raw in spec["case"]["gmr_values"].items():
        items = raw if isinstance(raw, list) else [raw]
        controlled = next((x.get("vocabulary_code") for x in items if isinstance(x, dict) and x.get("vocabulary_code")), None)
        fields.append({"field_key": key, "applies_to": "CASE_EVENT,ALL", "controlled_vocabulary": controlled})
        for item in items:
            if isinstance(item, dict) and item.get("value_code") and controlled:
                marker = (controlled, item["value_code"])
                if marker not in seen_values:
                    vocab_values.append({"vocabulary_code": controlled, "value_code": item["value_code"], "status": "Active"})
                    seen_values.add(marker)
    for status in ("SOURCE_EXPLICIT", "NORMALIZED", "EDITOR_INFERRED", "DISPUTED", "REJECTED", "SUPERSEDED"):
        vocab_values.append({"vocabulary_code": "assertion_status", "value_code": status, "status": "Active"})
    path.write_text(json.dumps({
        "registry": [{"registry_version": "1.0.0"}],
        "fields": fields,
        "vocabulary_values": vocab_values,
    }), encoding="utf-8")


def make_target(path):
    con = sqlite3.connect(path)
    con.executescript(TARGET_DDL)
    con.execute("INSERT INTO wp_module_meta VALUES('module_role','CE3_CE4_CE5_RICH_CONTENT')")
    con.execute("INSERT INTO wp_module_meta VALUES('module_version','test')")
    con.execute("INSERT INTO wp_encounter_class_policy VALUES('CE3',3,'#F2C94C','YELLOW','CE3PLUS',1,'CE3')")
    con.execute("INSERT INTO wp_encounter_class_policy VALUES('CE4',4,'#F2994A','ORANGE','CE3PLUS',1,'CE4')")
    con.commit()
    con.close()


class SingleCaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.work = Path(self.tempdir.name)
        asset_dir = self.work / "assets"
        asset_dir.mkdir()
        portrait_hash = make_png_1920x1080(asset_dir / "synthetic.png")
        self.spec = make_fixture_spec(portrait_hash)
        self.spec_path = self.work / "case.json"
        self.spec_path.write_text(json.dumps(self.spec), encoding="utf-8")
        self.gmr_path = self.work / "gmr.json"
        make_minimal_gmr(self.spec, self.gmr_path)
        self.package = self.work / "package.sqlite"
        single_case.build_package(self.spec_path, self.gmr_path, self.package, False)
        self.source = self.work / "source.sqlite"
        make_target(self.source)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_build_validate_and_insert(self):
        validation = single_case.inspect_package(self.package)
        self.assertEqual(validation["status"], "PASS")
        output = self.work / "successor.sqlite"
        result = single_case.apply_ce3plus(
            self.package, self.source, output, "UFO-TEST-CLEAR-LAKE-1989", None, False, None
        )
        self.assertEqual(result["result"], "INSERTED_ONE_CASE")
        con = sqlite3.connect(output)
        self.assertEqual(con.execute("SELECT count(*) FROM case_dossier").fetchone()[0], 1)
        self.assertEqual(con.execute("PRAGMA quick_check").fetchone()[0], "ok")
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])
        con.close()

    def test_identical_replay_is_noop(self):
        first = self.work / "first.sqlite"
        second = self.work / "second.sqlite"
        single_case.apply_ce3plus(self.package, self.source, first, "UFO-TEST-REPLAY", None, False, None)
        result = single_case.apply_ce3plus(self.package, first, second, "UFO-TEST-REPLAY", None, False, None)
        self.assertEqual(result["result"], "IDENTICAL_REPLAY_NOOP")
        con = sqlite3.connect(second)
        self.assertEqual(con.execute("SELECT count(*) FROM case_dossier").fetchone()[0], 1)
        con.close()

    def test_injected_failure_leaves_no_successor(self):
        output = self.work / "must-not-exist.sqlite"
        with self.assertRaisesRegex(single_case.CaseError, "injected failure"):
            single_case.apply_ce3plus(
                self.package, self.source, output, "UFO-TEST-ROLLBACK", None, False, "dossier"
            )
        self.assertFalse(output.exists())
        con = sqlite3.connect(self.source)
        self.assertEqual(con.execute("SELECT count(*) FROM case_dossier").fetchone()[0], 0)
        con.close()

    def test_source_hash_mismatch_stops_before_copy(self):
        output = self.work / "must-not-exist.sqlite"
        with self.assertRaisesRegex(single_case.CaseError, "hash differs"):
            single_case.apply_ce3plus(
                self.package, self.source, output, "UFO-TEST-HASH", "0" * 64, False, None
            )
        self.assertFalse(output.exists())

    def test_changed_replay_is_conflict(self):
        first = self.work / "first.sqlite"
        single_case.apply_ce3plus(self.package, self.source, first, "UFO-TEST-CONFLICT", None, False, None)
        changed = copy.deepcopy(self.spec)
        changed["case"]["narrative"] += " This sentence represents a deliberate changed replay."
        changed_spec = self.work / "changed.json"
        changed_spec.write_text(json.dumps(changed), encoding="utf-8")
        changed_package = self.work / "changed.sqlite"
        single_case.build_package(changed_spec, self.gmr_path, changed_package, False)
        with self.assertRaisesRegex(single_case.CaseError, "different content"):
            single_case.apply_ce3plus(
                changed_package, first, self.work / "conflict.sqlite", "UFO-TEST-CONFLICT", None, False, None
            )


if __name__ == "__main__":
    unittest.main()
