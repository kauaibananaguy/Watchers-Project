#!/usr/bin/env python3
"""Repair any failed GEIPAN case-page retrievals in a preserved snapshot.

The input snapshot is copied additively. Successful rows are byte-preserved;
only explicit non-DOWNLOADED rows are retried. Every attempt and result is
recorded, and the repaired package passes only when all 3,381 current public
case pages are present.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

import crawl_case_packets as shared


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def locate_one(root: Path, pattern: str) -> Path:
    matches = [path for path in root.rglob(pattern) if path.is_file()]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {pattern}; found {len(matches)}")
    return matches[0]


def add_repair_table(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS case_page_repair_attempt(
          repair_attempt_id INTEGER PRIMARY KEY,
          case_url TEXT NOT NULL REFERENCES case_page(case_url),
          attempted_at TEXT NOT NULL,
          prior_status TEXT NOT NULL,
          result_status TEXT NOT NULL,
          html_bytes INTEGER,
          html_sha256 TEXT,
          error TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_case_page_repair_result
          ON case_page_repair_attempt(result_status);
        """
    )


def repair(args: argparse.Namespace) -> dict[str, Any]:
    source_root = Path(args.input_dir)
    output_root = Path(args.output_dir)
    if output_root.exists():
        shutil.rmtree(output_root)
    shutil.copytree(source_root, output_root)

    database = locate_one(output_root, "GEIPAN_CASE_PAGE_SOURCE_SNAPSHOT_v0.2.0.sqlite")
    target_database = database.with_name("GEIPAN_CASE_PAGE_SOURCE_SNAPSHOT_v0.2.1_REPAIRED.sqlite")
    database.rename(target_database)
    database = target_database

    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=ON")
    add_repair_table(connection)
    rows = connection.execute(
        """
        SELECT case_url, source_case_id, source_locator, retrieval_status
        FROM case_page
        WHERE retrieval_status <> 'DOWNLOADED'
        ORDER BY case_url
        """
    ).fetchall()
    initial_remands = len(rows)
    repaired = 0
    unresolved = 0

    for index, (url, source_case_id, source_locator, prior_status) in enumerate(rows, start=1):
        result_status = "DOWNLOAD_ERROR"
        error = None
        html_bytes = None
        html_sha = None
        try:
            data, final_url, headers = shared.request_bytes(
                url, retries=args.retries, timeout=600
            )
            parsed = shared.parse_page(data, final_url)
            visible_text = parsed["text"]
            html_bytes = len(data)
            html_sha = hashlib.sha256(data).hexdigest()
            connection.execute("DELETE FROM case_page_field WHERE case_url=?", (url,))
            connection.execute("DELETE FROM case_asset_link WHERE case_url=?", (url,))
            connection.execute("DELETE FROM crawl_issue WHERE case_url=?", (url,))
            connection.execute(
                """
                UPDATE case_page
                SET final_url=?, retrieval_status='DOWNLOADED', retrieved_at=?,
                    http_headers_json=?, html_bytes=?, html_sha256=?, html_gzip=?,
                    page_title=?, meta_description=?, visible_text=?,
                    visible_text_sha256=?, error=NULL
                WHERE case_url=?
                """,
                (
                    final_url,
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    json.dumps(headers, ensure_ascii=False, sort_keys=True),
                    len(data),
                    html_sha,
                    gzip.compress(data, compresslevel=9),
                    parsed["title"],
                    parsed["meta_description"],
                    visible_text,
                    hashlib.sha256(visible_text.encode("utf-8")).hexdigest(),
                    url,
                ),
            )
            connection.executemany(
                "INSERT INTO case_page_field(case_url,source_label,source_value) VALUES(?,?,?)",
                [(url, row["label"], row["value"]) for row in parsed["fields"]],
            )
            assets: dict[str, tuple[str, str, int]] = {}
            for order, link in enumerate(parsed["links"], start=1):
                kind = shared.attachment_kind(link["url"], link["label"])
                if kind and link["url"] not in assets:
                    assets[link["url"]] = (link["label"], kind, order)
            connection.executemany(
                "INSERT INTO case_asset_link VALUES(?,?,?,?,?)",
                [
                    (url, asset_url, label, kind, order)
                    for asset_url, (label, kind, order) in assets.items()
                ],
            )
            result_status = "DOWNLOADED"
            repaired += 1
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
            connection.execute(
                "UPDATE case_page SET retrieval_status='DOWNLOAD_ERROR',error=? WHERE case_url=?",
                (error, url),
            )
            connection.execute(
                "INSERT INTO crawl_issue(case_url,issue_code,severity,detail) VALUES(?,?,?,?)",
                (url, "CASE_PAGE_REPAIR_FAILED", "HIGH", error),
            )
            unresolved += 1
        connection.execute(
            """
            INSERT INTO case_page_repair_attempt(
              case_url,attempted_at,prior_status,result_status,html_bytes,html_sha256,error
            ) VALUES(?,?,?,?,?,?,?)
            """,
            (
                url,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                prior_status,
                result_status,
                html_bytes,
                html_sha,
                error,
            ),
        )
        connection.commit()
        print(
            f"repair {index}/{initial_remands}: {url} -> {result_status}",
            flush=True,
        )
        time.sleep(args.delay_seconds)

    quick = connection.execute("PRAGMA quick_check").fetchone()[0]
    foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    total = connection.execute("SELECT COUNT(*) FROM case_page").fetchone()[0]
    status_counts = {
        str(key): int(value)
        for key, value in connection.execute(
            "SELECT retrieval_status,COUNT(*) FROM case_page GROUP BY retrieval_status"
        )
    }
    remaining = connection.execute(
        "SELECT COUNT(*) FROM case_page WHERE retrieval_status<>'DOWNLOADED'"
    ).fetchone()[0]
    asset_links = connection.execute("SELECT COUNT(*) FROM case_asset_link").fetchone()[0]
    fields = connection.execute("SELECT COUNT(*) FROM case_page_field").fetchone()[0]
    attempts = connection.execute(
        "SELECT COUNT(*) FROM case_page_repair_attempt"
    ).fetchone()[0]
    connection.close()

    summary = {
        "overall_status": (
            "PASS_ALL_CASE_PAGES_DOWNLOADED"
            if quick == "ok" and foreign_keys == 0 and total == args.expected_cases and remaining == 0
            else "FAIL_REMANDS_REMAIN"
        ),
        "expected_case_pages": args.expected_cases,
        "case_page_rows": total,
        "initial_remands": initial_remands,
        "repaired_this_run": repaired,
        "unresolved_this_run": unresolved,
        "remaining_remands": remaining,
        "case_page_status_counts": status_counts,
        "case_page_field_rows": fields,
        "case_asset_link_rows": asset_links,
        "repair_attempt_rows": attempts,
        "sqlite_quick_check": quick,
        "foreign_key_violations": foreign_keys,
    }
    (output_root / "REPAIR_SUMMARY.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    with (output_root / "REPAIR_QUEUE.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        con = sqlite3.connect(database)
        cursor = con.execute(
            """
            SELECT case_url,source_case_id,retrieval_status,error
            FROM case_page
            WHERE retrieval_status<>'DOWNLOADED'
            ORDER BY case_url
            """
        )
        writer = csv.writer(handle)
        writer.writerow([item[0] for item in cursor.description])
        writer.writerows(cursor)
        con.close()
    checksums = []
    for path in sorted(output_root.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            checksums.append(
                f"{sha256(path)}  {path.relative_to(output_root).as_posix()}"
            )
    (output_root / "SHA256SUMS.txt").write_text(
        "\n".join(checksums) + "\n", encoding="utf-8"
    )
    if summary["overall_status"] != "PASS_ALL_CASE_PAGES_DOWNLOADED":
        raise SystemExit(json.dumps(summary, indent=2, ensure_ascii=False))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--expected-cases", type=int, default=3381)
    parser.add_argument("--delay-seconds", type=float, default=30.0)
    parser.add_argument("--retries", type=int, default=12)
    result = repair(parser.parse_args())
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
