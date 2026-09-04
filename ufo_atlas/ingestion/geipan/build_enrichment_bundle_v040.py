#!/usr/bin/env python3
"""Build a validated, unnested GEIPAN enrichment-input bundle.

Inputs are the preserved current/legacy source snapshot, complete case-page
snapshot, linked-asset metadata/text snapshot, and source-feature assertions.
The output remains source-neutral: GEIPAN is provenance, not a public parent
hierarchy, and no central Atlas identities are allocated here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any


EXPECTED = {
    "physical_source_rows": 17573,
    "current_case_rows": 3381,
    "current_testimony_rows": 6068,
    "unique_case_ids": 3392,
    "unique_testimony_keys": 6176,
    "case_page_rows": 3381,
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def locate_one(root: Path, pattern: str) -> Path:
    matches = [path for path in root.rglob(pattern) if path.is_file()]
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one {pattern!r} below {root}; found {len(matches)}")
    return matches[0]


def load_json_candidates(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    rows: list[tuple[Path, dict[str, Any]]] = []
    for path in root.rglob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(payload, dict):
            rows.append((path, payload))
    return rows


def find_source_validation(root: Path) -> tuple[Path, dict[str, Any]]:
    candidates = []
    for path, payload in load_json_candidates(root):
        if payload.get("source_record_count") == EXPECTED["physical_source_rows"]:
            candidates.append((path, payload))
    if not candidates:
        raise RuntimeError("No GEIPAN v0.2 source validation report with 17,573 source rows was found")
    candidates.sort(key=lambda item: ("VALIDATION_REPORT" not in item[0].name, len(str(item[0]))))
    return candidates[0]


def sqlite_check(path: Path) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    quick = con.execute("PRAGMA quick_check").fetchone()[0]
    fk = len(con.execute("PRAGMA foreign_key_check").fetchall())
    tables = {
        row[0]
        for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    result = {
        "path": str(path),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "quick_check": quick,
        "foreign_key_violations": fk,
        "tables": sorted(tables),
    }
    con.close()
    if quick != "ok" or fk:
        raise RuntimeError(f"SQLite validation failed for {path}: quick={quick!r}, fk={fk}")
    return result


def query_counts(path: Path, table: str, status_column: str | None = None) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if table not in tables:
        con.close()
        raise RuntimeError(f"Required table {table!r} is missing from {path}")
    total = int(con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
    statuses: dict[str, int] = {}
    if status_column:
        columns = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
        if status_column not in columns:
            con.close()
            raise RuntimeError(f"Required column {status_column!r} is missing from {table!r}")
        statuses = {
            str(key): int(value)
            for key, value in con.execute(
                f'SELECT "{status_column}",COUNT(*) FROM "{table}" GROUP BY "{status_column}"'
            )
        }
    con.close()
    return {"total": total, "statuses": statuses}


def candidate_count(path: Path, table_names: tuple[str, ...], column_names: tuple[str, ...], value: str) -> int | None:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    tables = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    result: int | None = None
    for table in table_names:
        if table not in tables:
            continue
        columns = {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}
        column = next((name for name in column_names if name in columns), None)
        if column:
            result = int(
                con.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE "{column}"=?', (value,)
                ).fetchone()[0]
            )
            break
    con.close()
    return result


def copy_tree_without_archives(source: Path, destination: Path) -> list[dict[str, Any]]:
    copied: list[dict[str, Any]] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        # The bundle must be a single logical package, not a stack of archives.
        if path.suffix.lower() in {".zip", ".7z", ".rar", ".tar", ".tgz"}:
            continue
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(
            {
                "path": target.relative_to(destination.parent).as_posix(),
                "bytes": target.stat().st_size,
                "sha256": sha256(target),
            }
        )
    return copied


def build(args: argparse.Namespace) -> dict[str, Any]:
    roots = {
        "source_snapshot": Path(args.source_snapshot),
        "case_pages": Path(args.case_pages),
        "assets": Path(args.assets),
        "features": Path(args.features),
    }
    for name, root in roots.items():
        if not root.exists():
            raise FileNotFoundError(f"Missing {name} input: {root}")

    output = Path(args.output_dir)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    source_report_path, source_report = find_source_validation(roots["source_snapshot"])
    source_dbs = [path for path in roots["source_snapshot"].rglob("*.sqlite") if path.is_file()]
    if len(source_dbs) != 1:
        raise RuntimeError(f"Expected one source-snapshot database; found {len(source_dbs)}")
    source_db = source_dbs[0]
    page_db = locate_one(roots["case_pages"], "GEIPAN_CASE_PAGE_SOURCE_SNAPSHOT_v0.2.0.sqlite")
    asset_db = locate_one(roots["assets"], "GEIPAN_LINKED_ASSET_METADATA_AND_TEXT_v0.1.0.sqlite")
    feature_db = locate_one(roots["features"], "GEIPAN_SOURCE_FEATURE_ASSERTIONS_v0.1.0.sqlite")

    component_checks = {
        "source_snapshot": sqlite_check(source_db),
        "case_pages": sqlite_check(page_db),
        "assets": sqlite_check(asset_db),
        "features": sqlite_check(feature_db),
    }

    required_source_values = {
        "source_record_count": EXPECTED["physical_source_rows"],
        "current_case_export_rows": EXPECTED["current_case_rows"],
        "current_testimony_export_rows": EXPECTED["current_testimony_rows"],
        "unique_case_ids_across_exports": EXPECTED["unique_case_ids"],
        "unique_testimony_title_keys_across_exports": EXPECTED["unique_testimony_keys"],
    }
    source_mismatches = {
        key: {"expected": expected, "actual": source_report.get(key)}
        for key, expected in required_source_values.items()
        if source_report.get(key) != expected
    }
    if source_mismatches:
        raise RuntimeError(f"Source snapshot census mismatch: {source_mismatches}")

    pages = query_counts(page_db, "case_page", "retrieval_status")
    if pages["total"] != EXPECTED["case_page_rows"]:
        raise RuntimeError(
            f"Case-page count mismatch: expected {EXPECTED['case_page_rows']}, found {pages['total']}"
        )
    page_fields = query_counts(page_db, "case_page_field")
    page_asset_links = query_counts(page_db, "case_asset_link")

    assets = query_counts(asset_db, "asset", "retrieval_status")
    asset_text_units = query_counts(asset_db, "asset_text_unit")
    asset_issues = query_counts(asset_db, "asset_issue")

    feature_docs = query_counts(feature_db, "source_document")
    feature_assertions = query_counts(feature_db, "feature_assertion")
    if feature_assertions["total"] <= 0:
        raise RuntimeError("Feature assertion database contains no assertions")

    page_remand_statuses = {
        key: value for key, value in pages["statuses"].items() if key != "DOWNLOADED" and value
    }
    asset_remand_statuses = {
        key: value for key, value in assets["statuses"].items() if key != "DOWNLOADED" and value
    }
    remands = {
        "case_page_statuses": page_remand_statuses,
        "asset_statuses": asset_remand_statuses,
        "asset_issue_rows": asset_issues["total"],
    }
    remand_count = sum(page_remand_statuses.values()) + sum(asset_remand_statuses.values())

    copied_files: list[dict[str, Any]] = []
    for name, root in roots.items():
        destination = output / name
        destination.mkdir(parents=True)
        copied_files.extend(copy_tree_without_archives(root, destination))

    nested_archives = [
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file() and path.suffix.lower() in {".zip", ".7z", ".rar", ".tar", ".tgz"}
    ]
    if nested_archives:
        raise RuntimeError(f"Nested archives found after copy: {nested_archives}")

    validation = {
        "overall_status": "PASS",
        "handoff_status": (
            "PASS_ENRICHMENT_INPUT_READY"
            if remand_count == 0 and asset_issues["total"] == 0
            else "PASS_WITH_EXPLICIT_SOURCE_REMANDS"
        ),
        "package_id": "UFO-ATLAS-GEIPAN-ENRICHMENT-INPUT-BUNDLE-0.4.0",
        "source_collection_id": "SRC-COLLECTION-GEIPAN",
        "controlling_architecture": "UFO Atlas GMR v1.0.0 and UFO-ATLAS-INT-STD-1.0.0",
        "source_role": "GEIPAN is source provenance, not a separate public Atlas hierarchy",
        "central_identity_allocated": False,
        "source_census": {
            "physical_source_rows": source_report["source_record_count"],
            "current_case_rows": source_report["current_case_export_rows"],
            "current_testimony_rows": source_report["current_testimony_export_rows"],
            "unique_case_ids": source_report["unique_case_ids_across_exports"],
            "unique_testimony_keys": source_report["unique_testimony_title_keys_across_exports"],
        },
        "case_page_census": {
            "case_pages": pages["total"],
            "status_counts": pages["statuses"],
            "source_field_rows": page_fields["total"],
            "asset_link_rows": page_asset_links["total"],
        },
        "asset_census": {
            "assets": assets["total"],
            "status_counts": assets["statuses"],
            "text_units": asset_text_units["total"],
            "issue_rows": asset_issues["total"],
        },
        "feature_census": {
            "source_documents": feature_docs["total"],
            "feature_assertions": feature_assertions["total"],
        },
        "source_remands": remands,
        "component_sqlite_validation": component_checks,
        "source_validation_report": str(source_report_path.relative_to(roots["source_snapshot"])),
        "copied_file_count_before_bundle_reports": len(copied_files),
        "nested_archives": nested_archives,
    }

    (output / "VALIDATION_REPORT.json").write_text(
        json.dumps(validation, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output / "SOURCE_COMPONENTS.json").write_text(
        json.dumps(
            {
                "source_snapshot_artifact_id": args.source_artifact_id,
                "case_page_run_id": args.case_page_run_id,
                "asset_run_id": args.asset_run_id,
                "feature_run_id": args.feature_run_id,
                "components": component_checks,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    readme = f"""# GEIPAN enrichment input bundle v0.4.0

Status: **{validation['handoff_status']}**

This package combines the validated current and legacy official GEIPAN exports,
all acquired official case pages, the linked-asset metadata and extracted text,
and provenance-preserving French source-feature assertions. It contains no
nested ZIP archive and allocates no central UFO Atlas identity.

## Census

- Physical source rows: **{source_report['source_record_count']:,}**
- Reconciled case identities: **{source_report['unique_case_ids_across_exports']:,}**
- Current official case pages: **{pages['total']:,}**
- Linked assets accounted for: **{assets['total']:,}**
- Source feature assertions: **{feature_assertions['total']:,}**

## Remaining boundary

Any non-DOWNLOADED page or asset state remains an explicit source remand. The
final Atlas import module may not claim full source completion until those
remands are repaired or formally adjudicated as unavailable after documented
attempts. GEIPAN classifications remain attributed source claims.
"""
    (output / "README_FIRST.md").write_text(readme, encoding="utf-8")

    checksums = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            checksums.append(
                f"{sha256(path)}  {path.relative_to(output).as_posix()}"
            )
    (output / "SHA256SUMS.txt").write_text(
        "\n".join(checksums) + "\n", encoding="utf-8"
    )
    return validation


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-snapshot", required=True)
    parser.add_argument("--case-pages", required=True)
    parser.add_argument("--assets", required=True)
    parser.add_argument("--features", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--source-artifact-id", default="")
    parser.add_argument("--case-page-run-id", default="")
    parser.add_argument("--asset-run-id", default="")
    parser.add_argument("--feature-run-id", default="")
    args = parser.parse_args()
    result = build(args)
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
