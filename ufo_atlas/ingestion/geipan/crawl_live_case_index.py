#!/usr/bin/env python3
"""Acquire the complete current GEIPAN public case index.

The crawler follows the official tabular search view, preserves every listed
case row in source order, and reconciles the current live index with the older
static downloadable case CSV. It does not create canonical Atlas identities.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

BASE_URL = "https://www.geipan.fr"
INDEX_URL = f"{BASE_URL}/fr/recherche/cas/tab"
STATS_URL = f"{BASE_URL}/fr/stats"
STATIC_CASES_URLS = [
    "https://www.cnes-geipan.fr/sites/default/files/Base_de_donn%C3%A9es_des_cas.csv",
    "https://www.geipan.fr/sites/default/files/Base_de_donn%C3%A9es_des_cas.csv",
]


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch(url: str, retries: int = 8) -> tuple[bytes, str, dict[str, str]]:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; Watchers-UFO-Atlas/1.0; public-source-preservation)",
                    "Accept": "text/html,application/xhtml+xml,text/csv,*/*",
                },
            )
            with urllib.request.urlopen(request, timeout=240) as response:
                data = response.read()
                if not data:
                    raise RuntimeError("empty response")
                return data, response.geturl(), dict(response.headers.items())
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < retries:
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    wait = min(60, 12 + attempt * 5)
                else:
                    wait = min(30, 2**attempt)
                time.sleep(wait)
    raise RuntimeError(f"Unable to fetch {url}: {last}")


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict[str, Any]] = []
        self.pager_hrefs: list[str] = []
        self._in_table = False
        self._in_tbody = False
        self._row: dict[str, Any] | None = None
        self._cell_class: str | None = None
        self._cell_text: list[str] = []
        self._cell_href: str | None = None
        self._cell_datetime: str | None = None
        self._anchor_text: list[str] | None = None
        self._anchor_href: str | None = None

    @staticmethod
    def attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str | None]:
        return dict(attrs)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = self.attrs(attrs)
        classes = str(values.get("class") or "")
        if tag == "table" and "views-table" in classes:
            self._in_table = True
        elif self._in_table and tag == "tbody":
            self._in_tbody = True
        elif self._in_tbody and tag == "tr":
            self._row = {}
        elif self._row is not None and tag == "td":
            self._cell_class = classes
            self._cell_text = []
            self._cell_href = None
            self._cell_datetime = None
        elif self._cell_class is not None and tag == "a" and values.get("href"):
            self._cell_href = str(values["href"])
        elif self._cell_class is not None and tag == "time":
            self._cell_datetime = str(values.get("datetime") or "") or None
        elif tag == "a" and values.get("href"):
            self._anchor_text = []
            self._anchor_href = str(values["href"])

    def handle_data(self, data: str) -> None:
        if self._cell_class is not None:
            self._cell_text.append(data)
        if self._anchor_text is not None:
            self._anchor_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor_text is not None:
            text = " ".join("".join(self._anchor_text).split())
            if "page=" in str(self._anchor_href) or "Page" in text or "page" in text:
                self.pager_hrefs.append(str(self._anchor_href))
            self._anchor_text = None
            self._anchor_href = None
        if self._cell_class is not None and tag == "td" and self._row is not None:
            text = " ".join("".join(self._cell_text).split())
            key = self._cell_class
            if "views-field-title" in key:
                self._row["case_title"] = text
            elif "field-date-d-observation-textuel" in key:
                self._row["observation_date_original"] = text
            elif "field-classification-des-cas" in key:
                self._row["classification_original"] = text
            elif "field-departement-textuel" in key:
                self._row["department_original"] = text
            elif "field-phenomene" in key:
                self._row["phenomenon_original"] = text
            elif "views-field-changed" in key:
                self._row["updated_date_original"] = text
                self._row["updated_datetime"] = self._cell_datetime
            elif "field-date-d-observation" in key:
                self._row["observation_date_normalized"] = text
            elif "views-field-view-node" in key:
                self._row["case_path"] = self._cell_href
            self._cell_class = None
            self._cell_text = []
            self._cell_href = None
            self._cell_datetime = None
        elif tag == "tr" and self._row is not None:
            if self._row.get("case_path"):
                self.rows.append(self._row)
            self._row = None
        elif tag == "tbody" and self._in_tbody:
            self._in_tbody = False
        elif tag == "table" and self._in_table:
            self._in_table = False


def parse_index(data: bytes) -> tuple[list[dict[str, Any]], list[str]]:
    parser = TableParser()
    parser.feed(data.decode("utf-8", errors="replace"))
    return parser.rows, parser.pager_hrefs


def last_page_from_pager(hrefs: list[str]) -> int:
    pages: list[int] = []
    for href in hrefs:
        parsed = urllib.parse.urlparse(href)
        values = urllib.parse.parse_qs(parsed.query)
        for raw in values.get("page", []):
            for candidate in re.findall(r"\d+", raw):
                pages.append(int(candidate))
    if not pages:
        raise RuntimeError("Unable to determine GEIPAN pager range")
    return max(pages)


def parse_live_stats(data: bytes) -> dict[str, Any]:
    text = data.decode("utf-8", errors="replace")
    total_match = re.search(
        r'class="stat-total"[^>]*>\s*([0-9][0-9\s]*)\s*Cas\s*-\s*Statistiques\s+du\s+([0-9/]+)',
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    classes: dict[str, int] = {}
    for count, code in re.findall(
        r'class="stat-legende-number"[^>]*>\s*\(([0-9][0-9\s]*)\s*Cas\s*([ABCD])\)',
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        classes[code.upper()] = int(re.sub(r"\s+", "", count))
    return {
        "published_case_count": int(re.sub(r"\s+", "", total_match.group(1))) if total_match else None,
        "statistics_date_original": total_match.group(2) if total_match else None,
        "classification_counts": classes,
    }


def load_static_case_ids() -> tuple[set[str], dict[str, Any]]:
    errors: list[str] = []
    data: bytes | None = None
    final_url = ""
    headers: dict[str, str] = {}
    for url in STATIC_CASES_URLS:
        try:
            data, final_url, headers = fetch(url)
            break
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
    if data is None:
        raise RuntimeError("Unable to retrieve static GEIPAN case CSV:\n" + "\n".join(errors))
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(text.splitlines(), delimiter=";", quotechar='"')
    ids = {
        str(row.get("cas_numEtude") or "").strip()
        for row in reader
        if str(row.get("cas_numEtude") or "").strip()
    }
    return ids, {
        "final_url": final_url,
        "bytes": len(data),
        "sha256": sha256_bytes(data),
        "headers": headers,
        "case_id_count": len(ids),
    }


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def build(args: argparse.Namespace) -> None:
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    acquired_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    stats_data, stats_final_url, stats_headers = fetch(STATS_URL)
    live_stats = parse_live_stats(stats_data)
    expected_count = live_stats.get("published_case_count")
    if not isinstance(expected_count, int):
        raise RuntimeError("Could not parse current GEIPAN published-case count")

    first_data, first_final_url, first_headers = fetch(f"{INDEX_URL}?page=0")
    first_rows, pager_hrefs = parse_index(first_data)
    last_page = last_page_from_pager(pager_hrefs)

    page_inventory: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []
    for page_number in range(last_page + 1):
        if page_number == 0:
            data, final_url, headers = first_data, first_final_url, first_headers
            parsed_rows = first_rows
        else:
            data, final_url, headers = fetch(f"{INDEX_URL}?page={page_number}")
            parsed_rows, _ = parse_index(data)
        page_inventory.append(
            {
                "page_number": page_number,
                "requested_url": f"{INDEX_URL}?page={page_number}",
                "final_url": final_url,
                "bytes": len(data),
                "sha256": sha256_bytes(data),
                "row_count": len(parsed_rows),
                "headers": headers,
            }
        )
        for row_number, row in enumerate(parsed_rows, start=1):
            path = str(row["case_path"])
            case_id = path.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
            absolute_url = urllib.parse.urljoin(BASE_URL, path.split("?", 1)[0])
            raw_payload = json.dumps(row, ensure_ascii=False, sort_keys=True)
            rows.append(
                {
                    "source_collection_id": "SRC-COLLECTION-GEIPAN",
                    "source_sequence": len(rows) + 1,
                    "source_record_id": f"GEIPAN-LIVE-CASE-{case_id}",
                    "geipan_case_id": case_id,
                    "page_number": page_number,
                    "row_number_on_page": row_number,
                    "source_locator": f"{INDEX_URL}?page={page_number}#row-{row_number}",
                    "case_url": absolute_url,
                    "case_title": row.get("case_title"),
                    "observation_date_original": row.get("observation_date_original"),
                    "observation_date_normalized": row.get("observation_date_normalized"),
                    "classification_original": row.get("classification_original"),
                    "department_original": row.get("department_original"),
                    "phenomenon_original": row.get("phenomenon_original"),
                    "updated_date_original": row.get("updated_date_original"),
                    "updated_datetime": row.get("updated_datetime"),
                    "raw_payload": raw_payload,
                    "row_sha256": sha256_bytes(raw_payload.encode("utf-8")),
                }
            )
        print(f"page {page_number + 1}/{last_page + 1}: {len(parsed_rows)} rows; cumulative {len(rows)}", flush=True)
        if page_number < last_page:
            time.sleep(args.delay_seconds)

    paths = [row["case_url"] for row in rows]
    ids = [row["geipan_case_id"] for row in rows]
    duplicate_urls = [url for url, count in Counter(paths).items() if count > 1]
    duplicate_ids = [case_id for case_id, count in Counter(ids).items() if count > 1]
    static_ids, static_profile = load_static_case_ids()
    live_ids = set(ids)
    exact_static_overlap = sorted(live_ids & static_ids)
    live_only = sorted(live_ids - static_ids)
    static_only = sorted(static_ids - live_ids)

    for row in rows:
        row["static_csv_exact_id_match"] = row["geipan_case_id"] in static_ids
        row["source_resolution_proposal"] = (
            "PROPOSE_SOURCE_VARIANT" if row["static_csv_exact_id_match"] else "PROPOSE_NEW_CANONICAL"
        )

    validation = {
        "overall_status": "PASS" if (
            len(rows) == expected_count
            and not duplicate_urls
            and not duplicate_ids
            and len(page_inventory) == last_page + 1
            and all(page["row_count"] > 0 for page in page_inventory)
        ) else "FAIL",
        "stage": "CURRENT_LIVE_CASE_INDEX_ACQUISITION",
        "acquired_at": acquired_at,
        "expected_live_case_count": expected_count,
        "live_case_index_count": len(rows),
        "last_page_zero_based": last_page,
        "page_count": len(page_inventory),
        "duplicate_case_urls": len(duplicate_urls),
        "duplicate_case_ids": len(duplicate_ids),
        "static_case_ids": len(static_ids),
        "exact_static_overlap": len(exact_static_overlap),
        "live_only_case_ids": len(live_only),
        "static_only_case_ids": len(static_only),
        "classification_counts_index": dict(Counter(row["classification_original"] for row in rows)),
        "classification_counts_stats_page": live_stats.get("classification_counts"),
        "sqlite_quick_check": None,
        "foreign_key_violations": None,
        "next_stage": "DOWNLOAD_AND_TRANSFORM_ALL_CURRENT_CASE_DETAIL_PAGES_AND_ATTACHMENTS",
    }
    if validation["overall_status"] != "PASS":
        (output / "VALIDATION_REPORT.json").write_text(
            json.dumps(validation, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        raise RuntimeError(json.dumps(validation, indent=2, ensure_ascii=False))

    write_jsonl(output / "LIVE_CASE_INDEX.jsonl", rows)
    columns = list(rows[0]) if rows else []
    with (output / "LIVE_CASE_INDEX.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    (output / "PAGE_INVENTORY.json").write_text(json.dumps(page_inventory, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (output / "LIVE_STATS_CENSUS.json").write_text(
        json.dumps(
            {
                **live_stats,
                "final_url": stats_final_url,
                "headers": stats_headers,
                "bytes": len(stats_data),
                "sha256": sha256_bytes(stats_data),
            },
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )
    (output / "STATIC_EXPORT_RECONCILIATION.json").write_text(
        json.dumps(
            {
                "static_source": static_profile,
                "static_case_ids": len(static_ids),
                "live_case_ids": len(live_ids),
                "exact_static_overlap": len(exact_static_overlap),
                "live_only_case_ids": live_only,
                "static_only_case_ids": static_only,
            },
            indent=2,
            ensure_ascii=False,
        ) + "\n",
        encoding="utf-8",
    )

    database = output / "GEIPAN_LIVE_CASE_INDEX_v0.1.0.sqlite"
    if database.exists():
        database.unlink()
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(
        """
        CREATE TABLE page_inventory(
          page_number INTEGER PRIMARY KEY,
          requested_url TEXT NOT NULL,
          final_url TEXT NOT NULL,
          bytes INTEGER NOT NULL,
          sha256 TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          headers_json TEXT NOT NULL
        );
        CREATE TABLE live_case_index(
          source_collection_id TEXT NOT NULL,
          source_sequence INTEGER NOT NULL UNIQUE,
          source_record_id TEXT PRIMARY KEY,
          geipan_case_id TEXT NOT NULL UNIQUE,
          page_number INTEGER NOT NULL REFERENCES page_inventory(page_number),
          row_number_on_page INTEGER NOT NULL,
          source_locator TEXT NOT NULL,
          case_url TEXT NOT NULL UNIQUE,
          case_title TEXT NOT NULL,
          observation_date_original TEXT,
          observation_date_normalized TEXT,
          classification_original TEXT,
          department_original TEXT,
          phenomenon_original TEXT,
          updated_date_original TEXT,
          updated_datetime TEXT,
          raw_payload TEXT NOT NULL,
          row_sha256 TEXT NOT NULL,
          static_csv_exact_id_match INTEGER NOT NULL,
          source_resolution_proposal TEXT NOT NULL
        );
        CREATE INDEX idx_geipan_live_date ON live_case_index(observation_date_normalized);
        CREATE INDEX idx_geipan_live_classification ON live_case_index(classification_original);
        CREATE INDEX idx_geipan_live_department ON live_case_index(department_original);
        CREATE INDEX idx_geipan_live_phenomenon ON live_case_index(phenomenon_original);
        """
    )
    connection.executemany(
        "INSERT INTO page_inventory VALUES (?,?,?,?,?,?,?)",
        [
            (
                page["page_number"], page["requested_url"], page["final_url"],
                page["bytes"], page["sha256"], page["row_count"],
                json.dumps(page["headers"], ensure_ascii=False, sort_keys=True),
            )
            for page in page_inventory
        ],
    )
    placeholders = ",".join("?" for _ in columns)
    connection.executemany(
        f"INSERT INTO live_case_index VALUES ({placeholders})",
        [
            tuple(int(row[column]) if column == "static_csv_exact_id_match" else row[column] for column in columns)
            for row in rows
        ],
    )
    connection.commit()
    quick = connection.execute("PRAGMA quick_check").fetchone()[0]
    foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    connection.close()
    validation["sqlite_quick_check"] = quick
    validation["foreign_key_violations"] = foreign_keys
    if quick != "ok" or foreign_keys:
        validation["overall_status"] = "FAIL"
        raise RuntimeError(json.dumps(validation, indent=2, ensure_ascii=False))
    (output / "VALIDATION_REPORT.json").write_text(json.dumps(validation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    readme = f"""# GEIPAN current live case index — v0.1.0

Status: **PASS**  
Acquired: `{acquired_at}`

- Current published cases: **{len(rows):,}**
- Tabular search pages: **{len(page_inventory):,}**
- Exact matches to the older static case export: **{len(exact_static_overlap):,}**
- Current live cases absent from the older static export: **{len(live_only):,}**
- Static-export IDs not present in the current live index: **{len(static_only):,}**

This is a source-index checkpoint for the one source-neutral UFO Atlas. It preserves each current GEIPAN case listing and its stable public URL without making irreversible canonical merges.

The next stage downloads every current case page, source packet, attachment, image, and testimony link that GEIPAN exposes publicly, then maps the recovered values into the controlling UFO Atlas GMR.
"""
    (output / "README_FIRST.md").write_text(readme, encoding="utf-8")

    manifest_files = []
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name != "manifest.json":
            manifest_files.append({"path": path.name, "bytes": path.stat().st_size, "sha256": sha256_file(path)})
    manifest = {
        "package_id": "UFO-ATLAS-GEIPAN-LIVE-CASE-INDEX-0.1.0",
        "source_collection_id": "SRC-COLLECTION-GEIPAN",
        "created_at": acquired_at,
        "files": manifest_files,
        "manifest_note": "Manifest excludes its own self-hash.",
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    package = Path(args.package)
    package.unlink(missing_ok=True)
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for path in sorted(output.iterdir()):
            if path.is_file():
                archive.write(path, path.name)
    with zipfile.ZipFile(package) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP integrity failure at {bad}")
        if any(name.lower().endswith(".zip") for name in archive.namelist()):
            raise RuntimeError("Nested ZIP detected")
    Path(str(package) + ".sha256").write_text(
        f"{sha256_file(package)}  {package.name}\n",
        encoding="utf-8",
    )
    print(json.dumps(validation, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="live_index_output")
    parser.add_argument("--package", default="GEIPAN_LIVE_CASE_INDEX_v0.1.0.zip")
    parser.add_argument("--delay-seconds", type=float, default=0.2)
    build(parser.parse_args())


if __name__ == "__main__":
    main()
