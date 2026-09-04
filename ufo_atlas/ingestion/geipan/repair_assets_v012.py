#!/usr/bin/env python3
"""Repair non-downloaded GEIPAN linked assets and merge the results additively.

All prior metadata is preserved. Only explicit non-DOWNLOADED assets are retried.
Permanent HTTP absence is recorded as a terminal source state rather than being
silently discarded. Transient failures remain blocking remands.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any


TERMINAL_STATES = {
    "DOWNLOADED",
    "SOURCE_NOT_AVAILABLE_404",
    "SOURCE_FORBIDDEN_403",
    "SOURCE_GONE_410",
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
        raise RuntimeError(f"Expected one {pattern!r}; found {len(matches)}")
    return matches[0]


def plan(args: argparse.Namespace) -> None:
    root = Path(args.input_dir)
    database = locate_one(root, "GEIPAN_LINKED_ASSET_METADATA_AND_TEXT_v0.1.0.sqlite")
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    rows = [
        dict(row)
        for row in connection.execute(
            """
            SELECT asset_url,inferred_kind,linked_case_count,case_urls,link_labels,
                   retrieval_status AS prior_status,error AS prior_error
            FROM asset
            WHERE retrieval_status <> 'DOWNLOADED'
            ORDER BY asset_url
            """
        )
    ]
    total_assets = connection.execute("SELECT COUNT(*) FROM asset").fetchone()[0]
    prior_status_counts = {
        str(key): int(value)
        for key, value in connection.execute(
            "SELECT retrieval_status,COUNT(*) FROM asset GROUP BY retrieval_status"
        )
    }
    connection.close()

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "repair_assets.json").write_text(
        json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    include = []
    if rows:
        for start in range(0, len(rows), args.batch_size):
            include.append(
                {
                    "batch_index": start // args.batch_size,
                    "start": start,
                    "end": min(start + args.batch_size, len(rows)),
                    "expected_assets": min(args.batch_size, len(rows) - start),
                }
            )
    else:
        include.append(
            {"batch_index": 0, "start": 0, "end": 0, "expected_assets": 0}
        )
    matrix = {"include": include}
    (output / "matrix.json").write_text(
        json.dumps(matrix, separators=(",", ":")), encoding="utf-8"
    )
    summary = {
        "status": "PASS",
        "total_assets": int(total_assets),
        "repair_queue_count": len(rows),
        "prior_status_counts": prior_status_counts,
        "batch_size": args.batch_size,
        "batch_count": len(include),
    }
    (output / "PLAN_SUMMARY.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    print(json.dumps(matrix, separators=(",", ":")))


def map_terminal_status(status: str, error: str | None) -> str:
    if status == "DOWNLOADED":
        return status
    text = (error or "").lower()
    if "http error 404" in text or "status code 404" in text:
        return "SOURCE_NOT_AVAILABLE_404"
    if "http error 403" in text or "status code 403" in text:
        return "SOURCE_FORBIDDEN_403"
    if "http error 410" in text or "status code 410" in text:
        return "SOURCE_GONE_410"
    return status


def merge(args: argparse.Namespace) -> dict[str, Any]:
    original_root = Path(args.original_dir)
    repairs_root = Path(args.repairs_dir)
    output_root = Path(args.output_dir)
    if output_root.exists():
        shutil.rmtree(output_root)
    shutil.copytree(original_root, output_root)

    original_database = locate_one(
        output_root, "GEIPAN_LINKED_ASSET_METADATA_AND_TEXT_v0.1.0.sqlite"
    )
    database = original_database.with_name(
        "GEIPAN_LINKED_ASSET_METADATA_AND_TEXT_v0.1.2_REPAIRED.sqlite"
    )
    original_database.rename(database)

    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS asset_repair_attempt(
          repair_attempt_id INTEGER PRIMARY KEY,
          asset_url TEXT NOT NULL REFERENCES asset(asset_url),
          prior_status TEXT NOT NULL,
          result_status TEXT NOT NULL,
          repair_shard_name TEXT,
          byte_count INTEGER,
          sha256 TEXT,
          error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_asset_repair_result
          ON asset_repair_attempt(result_status);
        """
    )

    repair_db_paths = sorted(repairs_root.rglob("GEIPAN_ASSET_BATCH_*.sqlite"))
    repaired_rows = 0
    for repair_db in repair_db_paths:
        repair_connection = sqlite3.connect(
            f"file:{repair_db}?mode=ro", uri=True
        )
        repair_connection.row_factory = sqlite3.Row
        batch_index = repair_db.stem.split("_")[-1]
        shard_name = f"geipan-asset-repair-batch-{batch_index}"
        for repair_row in repair_connection.execute("SELECT * FROM asset"):
            data = dict(repair_row)
            url = data["asset_url"]
            current = connection.execute(
                "SELECT retrieval_status,error FROM asset WHERE asset_url=?", (url,)
            ).fetchone()
            if current is None:
                raise RuntimeError(f"Repair result has no original asset row: {url}")
            prior_status = str(current[0])
            mapped_status = map_terminal_status(
                str(data["retrieval_status"]), data.get("error")
            )
            connection.execute(
                "DELETE FROM asset_text_unit WHERE asset_url=?", (url,)
            )
            connection.execute("DELETE FROM asset_issue WHERE asset_url=?", (url,))
            connection.execute(
                """
                UPDATE asset
                SET inferred_kind=?,linked_case_count=?,case_urls=?,link_labels=?,
                    retrieval_status=?,retrieved_at=?,final_url=?,http_headers_json=?,
                    mime_type=?,byte_count=?,sha256=?,source_shard_name=?,
                    source_shard_blob_path=?,structure_json=?,error=?
                WHERE asset_url=?
                """,
                (
                    data.get("inferred_kind"),
                    data.get("linked_case_count"),
                    data.get("case_urls"),
                    data.get("link_labels"),
                    mapped_status,
                    data.get("retrieved_at"),
                    data.get("final_url"),
                    data.get("http_headers_json"),
                    data.get("mime_type"),
                    data.get("byte_count"),
                    data.get("sha256"),
                    shard_name,
                    data.get("local_blob_path"),
                    data.get("structure_json"),
                    data.get("error"),
                    url,
                ),
            )
            for unit in repair_connection.execute(
                """
                SELECT unit_number,unit_type,text,text_sha256,error
                FROM asset_text_unit WHERE asset_url=?
                """,
                (url,),
            ):
                connection.execute(
                    """
                    INSERT OR IGNORE INTO asset_text_unit(
                      asset_url,unit_number,unit_type,text,text_sha256,error
                    ) VALUES(?,?,?,?,?,?)
                    """,
                    (url, *tuple(unit)),
                )
            if mapped_status not in TERMINAL_STATES:
                connection.execute(
                    """
                    INSERT INTO asset_issue(asset_url,issue_code,severity,detail)
                    VALUES(?,?,?,?)
                    """,
                    (
                        url,
                        "ASSET_REPAIR_REMAINS_UNRESOLVED",
                        "HIGH",
                        data.get("error") or mapped_status,
                    ),
                )
            connection.execute(
                """
                INSERT INTO asset_repair_attempt(
                  asset_url,prior_status,result_status,repair_shard_name,
                  byte_count,sha256,error
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    url,
                    prior_status,
                    mapped_status,
                    shard_name,
                    data.get("byte_count"),
                    data.get("sha256"),
                    data.get("error"),
                ),
            )
            repaired_rows += 1
        repair_connection.close()
        connection.commit()

    quick = connection.execute("PRAGMA quick_check").fetchone()[0]
    foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    total_assets = connection.execute("SELECT COUNT(*) FROM asset").fetchone()[0]
    status_counts = {
        str(key): int(value)
        for key, value in connection.execute(
            "SELECT retrieval_status,COUNT(*) FROM asset GROUP BY retrieval_status"
        )
    }
    blocking = connection.execute(
        """
        SELECT COUNT(*) FROM asset
        WHERE retrieval_status NOT IN (
          'DOWNLOADED','SOURCE_NOT_AVAILABLE_404','SOURCE_FORBIDDEN_403','SOURCE_GONE_410'
        )
        """
    ).fetchone()[0]
    source_unavailable = connection.execute(
        """
        SELECT COUNT(*) FROM asset
        WHERE retrieval_status IN (
          'SOURCE_NOT_AVAILABLE_404','SOURCE_FORBIDDEN_403','SOURCE_GONE_410'
        )
        """
    ).fetchone()[0]
    downloaded = connection.execute(
        "SELECT COUNT(*) FROM asset WHERE retrieval_status='DOWNLOADED'"
    ).fetchone()[0]
    text_units = connection.execute(
        "SELECT COUNT(*) FROM asset_text_unit"
    ).fetchone()[0]
    issue_rows = connection.execute("SELECT COUNT(*) FROM asset_issue").fetchone()[0]
    attempt_rows = connection.execute(
        "SELECT COUNT(*) FROM asset_repair_attempt"
    ).fetchone()[0]
    connection.close()

    summary = {
        "overall_status": (
            "PASS_ALL_ASSETS_TERMINALLY_ADJUDICATED"
            if quick == "ok"
            and foreign_keys == 0
            and total_assets == args.expected_assets
            and blocking == 0
            else "FAIL_BLOCKING_ASSET_REMANDS_REMAIN"
        ),
        "expected_assets": args.expected_assets,
        "asset_rows": total_assets,
        "downloaded_assets": downloaded,
        "source_unavailable_assets": source_unavailable,
        "blocking_remands": blocking,
        "status_counts": status_counts,
        "repair_result_rows": repaired_rows,
        "repair_attempt_rows": attempt_rows,
        "asset_text_units": text_units,
        "asset_issue_rows": issue_rows,
        "sqlite_quick_check": quick,
        "foreign_key_violations": foreign_keys,
        "terminal_source_policy": (
            "HTTP 403/404/410 after the configured retry wave is retained as an "
            "explicit source-unavailable state; other failures remain blocking."
        ),
    }
    (output_root / "REPAIR_SUMMARY.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    with (output_root / "REMAINING_REMANDS.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        con = sqlite3.connect(database)
        cursor = con.execute(
            """
            SELECT asset_url,retrieval_status,final_url,byte_count,sha256,error
            FROM asset
            WHERE retrieval_status NOT IN (
              'DOWNLOADED','SOURCE_NOT_AVAILABLE_404','SOURCE_FORBIDDEN_403','SOURCE_GONE_410'
            )
            ORDER BY asset_url
            """
        )
        writer = csv.writer(handle)
        writer.writerow([item[0] for item in cursor.description])
        writer.writerows(cursor)
        con.close()
    checksum_rows = []
    for path in sorted(output_root.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            checksum_rows.append(
                f"{sha256(path)}  {path.relative_to(output_root).as_posix()}"
            )
    (output_root / "SHA256SUMS.txt").write_text(
        "\n".join(checksum_rows) + "\n", encoding="utf-8"
    )
    if summary["overall_status"] != "PASS_ALL_ASSETS_TERMINALLY_ADJUDICATED":
        raise SystemExit(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("plan")
    p.add_argument("--input-dir", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--batch-size", type=int, default=25)
    p.set_defaults(function=plan)
    m = commands.add_parser("merge")
    m.add_argument("--original-dir", required=True)
    m.add_argument("--repairs-dir", required=True)
    m.add_argument("--output-dir", required=True)
    m.add_argument("--expected-assets", type=int, required=True)
    m.set_defaults(function=merge)
    args = parser.parse_args()
    result = args.function(args)
    if result is not None:
        print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
