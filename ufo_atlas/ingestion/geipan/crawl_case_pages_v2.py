#!/usr/bin/env python3
"""Acquire all official GEIPAN case pages and enumerate linked case assets.

This phase deliberately separates page preservation from binary-asset download.
It preserves the complete visible French page text, original HTML, source labels,
and every linked asset URL without assigning central Atlas identities.
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
from collections import Counter
from pathlib import Path
from typing import Any

import crawl_case_packets as shared


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_checksums(root: Path) -> None:
    rows = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            rows.append(f"{sha256_file(path)}  {path.relative_to(root).as_posix()}")
    (root / "SHA256SUMS.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")


def crawl(args: argparse.Namespace) -> None:
    rows = json.loads(Path(args.case_urls).read_text(encoding="utf-8"))
    subset = rows[args.start:args.end]
    out = Path(args.output_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    db = out / f"GEIPAN_CASE_PAGE_BATCH_{args.batch_index:04d}.sqlite"
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""
    CREATE TABLE case_page(
      case_url TEXT PRIMARY KEY,
      source_case_id TEXT,
      source_locator TEXT,
      final_url TEXT,
      retrieval_status TEXT NOT NULL,
      retrieved_at TEXT,
      http_headers_json TEXT,
      html_bytes INTEGER,
      html_sha256 TEXT,
      html_gzip BLOB,
      page_title TEXT,
      meta_description TEXT,
      visible_text TEXT,
      visible_text_sha256 TEXT,
      error TEXT
    );
    CREATE TABLE case_page_field(
      field_id INTEGER PRIMARY KEY,
      case_url TEXT NOT NULL REFERENCES case_page(case_url),
      source_label TEXT NOT NULL,
      source_value TEXT NOT NULL
    );
    CREATE TABLE case_asset_link(
      case_url TEXT NOT NULL REFERENCES case_page(case_url),
      asset_url TEXT NOT NULL,
      link_label TEXT,
      inferred_kind TEXT NOT NULL,
      source_order INTEGER NOT NULL,
      PRIMARY KEY(case_url,asset_url)
    );
    CREATE TABLE crawl_issue(
      issue_id INTEGER PRIMARY KEY,
      case_url TEXT NOT NULL,
      issue_code TEXT NOT NULL,
      severity TEXT NOT NULL,
      detail TEXT NOT NULL
    );
    CREATE INDEX idx_page_status ON case_page(retrieval_status);
    CREATE INDEX idx_asset_url ON case_asset_link(asset_url);
    """)
    statuses = Counter()
    for index, item in enumerate(subset, start=1):
        url = item["case_url"]
        try:
            data, final_url, headers = shared.request_bytes(url, retries=args.retries, timeout=300)
            parsed = shared.parse_page(data, final_url)
            visible = parsed["text"]
            con.execute(
                "INSERT INTO case_page VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    url,
                    item.get("source_case_id"),
                    item.get("source_locator"),
                    final_url,
                    "DOWNLOADED",
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    json.dumps(headers, ensure_ascii=False, sort_keys=True),
                    len(data),
                    hashlib.sha256(data).hexdigest(),
                    gzip.compress(data, compresslevel=9),
                    parsed["title"],
                    parsed["meta_description"],
                    visible,
                    hashlib.sha256(visible.encode("utf-8")).hexdigest(),
                    None,
                ),
            )
            con.executemany(
                "INSERT INTO case_page_field(case_url,source_label,source_value) VALUES(?,?,?)",
                [(url, row["label"], row["value"]) for row in parsed["fields"]],
            )
            assets: dict[str, tuple[str, str, int]] = {}
            for order, link in enumerate(parsed["links"], start=1):
                kind = shared.attachment_kind(link["url"], link["label"])
                if kind and link["url"] not in assets:
                    assets[link["url"]] = (link["label"], kind, order)
            con.executemany(
                "INSERT INTO case_asset_link VALUES(?,?,?,?,?)",
                [(url, asset_url, label, kind, order) for asset_url, (label, kind, order) in assets.items()],
            )
            statuses["DOWNLOADED"] += 1
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            con.execute(
                "INSERT INTO case_page(case_url,source_case_id,source_locator,retrieval_status,error) VALUES(?,?,?,?,?)",
                (url, item.get("source_case_id"), item.get("source_locator"), "DOWNLOAD_ERROR", detail),
            )
            con.execute(
                "INSERT INTO crawl_issue(case_url,issue_code,severity,detail) VALUES(?,?,?,?)",
                (url, "CASE_PAGE_DOWNLOAD_ERROR", "HIGH", detail),
            )
            statuses["DOWNLOAD_ERROR"] += 1
        con.commit()
        print(f"batch {args.batch_index}: {index}/{len(subset)} {dict(statuses)}", flush=True)
        time.sleep(args.delay_seconds)
    quick = con.execute("PRAGMA quick_check").fetchone()[0]
    fk = len(con.execute("PRAGMA foreign_key_check").fetchall())
    page_count = con.execute("SELECT COUNT(*) FROM case_page").fetchone()[0]
    unique_assets = con.execute("SELECT COUNT(DISTINCT asset_url) FROM case_asset_link").fetchone()[0]
    con.close()
    summary = {
        "batch_index": args.batch_index,
        "start": args.start,
        "end": args.end,
        "expected_cases": len(subset),
        "case_page_rows": page_count,
        "case_status_counts": dict(statuses),
        "unique_asset_urls": unique_assets,
        "sqlite_quick_check": quick,
        "foreign_key_violations": fk,
    }
    (out / "BATCH_SUMMARY.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_checksums(out)
    if quick != "ok" or fk or page_count != len(subset):
        raise SystemExit(json.dumps(summary, indent=2))


def aggregate(args: argparse.Namespace) -> None:
    src = Path(args.input_dir)
    out = Path(args.output_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    db = out / "GEIPAN_CASE_PAGE_SOURCE_SNAPSHOT_v0.2.0.sqlite"
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""
    CREATE TABLE case_page(
      case_url TEXT PRIMARY KEY,source_case_id TEXT,source_locator TEXT,final_url TEXT,
      retrieval_status TEXT NOT NULL,retrieved_at TEXT,http_headers_json TEXT,
      html_bytes INTEGER,html_sha256 TEXT,html_gzip BLOB,page_title TEXT,
      meta_description TEXT,visible_text TEXT,visible_text_sha256 TEXT,error TEXT
    );
    CREATE TABLE case_page_field(
      field_id INTEGER PRIMARY KEY,case_url TEXT NOT NULL REFERENCES case_page(case_url),
      source_label TEXT NOT NULL,source_value TEXT NOT NULL
    );
    CREATE TABLE case_asset_link(
      case_url TEXT NOT NULL REFERENCES case_page(case_url),asset_url TEXT NOT NULL,
      link_label TEXT,inferred_kind TEXT NOT NULL,source_order INTEGER NOT NULL,
      PRIMARY KEY(case_url,asset_url)
    );
    CREATE TABLE crawl_issue(
      issue_id INTEGER PRIMARY KEY,case_url TEXT NOT NULL,issue_code TEXT NOT NULL,
      severity TEXT NOT NULL,detail TEXT NOT NULL
    );
    CREATE INDEX idx_page_status ON case_page(retrieval_status);
    CREATE INDEX idx_asset_url ON case_asset_link(asset_url);
    """)
    batch_summaries: list[dict[str, Any]] = []
    for batch_db in sorted(src.rglob("GEIPAN_CASE_PAGE_BATCH_*.sqlite")):
        summary_path = batch_db.parent / "BATCH_SUMMARY.json"
        if summary_path.exists():
            batch_summaries.append(json.loads(summary_path.read_text(encoding="utf-8")))
        bcon = sqlite3.connect(f"file:{batch_db}?mode=ro", uri=True)
        bcon.row_factory = sqlite3.Row
        for row in bcon.execute("SELECT * FROM case_page"):
            con.execute("INSERT OR REPLACE INTO case_page VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row))
        for row in bcon.execute("SELECT case_url,source_label,source_value FROM case_page_field"):
            con.execute("INSERT INTO case_page_field(case_url,source_label,source_value) VALUES(?,?,?)", tuple(row))
        for row in bcon.execute("SELECT * FROM case_asset_link"):
            con.execute("INSERT OR IGNORE INTO case_asset_link VALUES(?,?,?,?,?)", tuple(row))
        for row in bcon.execute("SELECT case_url,issue_code,severity,detail FROM crawl_issue"):
            con.execute("INSERT INTO crawl_issue(case_url,issue_code,severity,detail) VALUES(?,?,?,?)", tuple(row))
        bcon.close()
        con.commit()
    quick = con.execute("PRAGMA quick_check").fetchone()[0]
    fk = len(con.execute("PRAGMA foreign_key_check").fetchall())
    page_count = con.execute("SELECT COUNT(*) FROM case_page").fetchone()[0]
    page_status = dict(con.execute("SELECT retrieval_status,COUNT(*) FROM case_page GROUP BY retrieval_status"))
    asset_links = con.execute("SELECT COUNT(*) FROM case_asset_link").fetchone()[0]
    unique_assets = con.execute("SELECT COUNT(DISTINCT asset_url) FROM case_asset_link").fetchone()[0]
    fields = con.execute("SELECT COUNT(*) FROM case_page_field").fetchone()[0]
    issues = con.execute("SELECT COUNT(*) FROM crawl_issue").fetchone()[0]
    summary = {
        "overall_status": "PASS" if quick == "ok" and fk == 0 and page_count == args.expected_cases else "FAIL",
        "expected_cases": args.expected_cases,
        "case_page_rows": page_count,
        "case_page_status_counts": page_status,
        "case_page_field_rows": fields,
        "case_asset_link_rows": asset_links,
        "unique_asset_urls": unique_assets,
        "crawl_issues": issues,
        "batch_count": len(batch_summaries),
        "sqlite_quick_check": quick,
        "foreign_key_violations": fk,
        "source_policy": "Official GEIPAN HTML and original French visible text are preserved without editorial replacement.",
    }
    (out / "SUMMARY.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    with (out / "CASE_PAGE_INDEX.csv").open("w", encoding="utf-8", newline="") as handle:
        cur = con.execute("SELECT case_url,source_case_id,final_url,retrieval_status,html_bytes,html_sha256,page_title,error FROM case_page ORDER BY case_url")
        writer = csv.writer(handle)
        writer.writerow([item[0] for item in cur.description])
        writer.writerows(cur)
    with (out / "ASSET_URL_INDEX.csv").open("w", encoding="utf-8", newline="") as handle:
        cur = con.execute("SELECT asset_url,MIN(inferred_kind),COUNT(*),GROUP_CONCAT(DISTINCT case_url) FROM case_asset_link GROUP BY asset_url ORDER BY asset_url")
        writer = csv.writer(handle)
        writer.writerow(["asset_url","inferred_kind","linked_case_count","case_urls"])
        writer.writerows(cur)
    con.close()
    write_checksums(out)
    if summary["overall_status"] != "PASS":
        raise SystemExit(json.dumps(summary, indent=2))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    c = commands.add_parser("crawl")
    c.add_argument("--case-urls", required=True)
    c.add_argument("--batch-index", type=int, required=True)
    c.add_argument("--start", type=int, required=True)
    c.add_argument("--end", type=int, required=True)
    c.add_argument("--output-dir", required=True)
    c.add_argument("--delay-seconds", type=float, default=8.0)
    c.add_argument("--retries", type=int, default=9)
    c.set_defaults(function=crawl)
    a = commands.add_parser("aggregate")
    a.add_argument("--input-dir", required=True)
    a.add_argument("--output-dir", required=True)
    a.add_argument("--expected-cases", type=int, default=3381)
    a.set_defaults(function=aggregate)
    return root


def main() -> None:
    args = parser().parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
