#!/usr/bin/env python3
"""Run GEIPAN acquisition with official-host fallbacks and resilient QA.

The official 2019 field-description workbook currently has malformed ZIP
component offsets. The raw workbook is preserved byte-for-byte; its parse
failure is recorded rather than allowed to erase the successfully acquired
case and testimony exports.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import acquire_geipan as base


CANDIDATES = {
    "GEIPAN_database_update_page.html": [
        "https://www.geipan.fr/fr/actualites/mise-a-jour-csv",
        "https://www.geipan.fr/fr/node/15",
        "https://geipan.fr/fr/node/15",
    ],
    "GEIPAN_cases.csv": [
        "https://www.cnes-geipan.fr/sites/default/files/Base_de_donn%C3%A9es_des_cas.csv",
        "https://www.geipan.fr/sites/default/files/Base_de_donn%C3%A9es_des_cas.csv",
        "https://geipan.fr/sites/default/files/Base_de_donn%C3%A9es_des_cas.csv",
    ],
    "GEIPAN_testimonies_observations.csv": [
        "https://www.cnes-geipan.fr/sites/default/files/Base_de_donn%C3%A9es_des_t%C3%A9moignages.csv",
        "https://www.geipan.fr/sites/default/files/Base_de_donn%C3%A9es_des_t%C3%A9moignages.csv",
        "https://geipan.fr/sites/default/files/Base_de_donn%C3%A9es_des_t%C3%A9moignages.csv",
    ],
    "GEIPAN_table_field_description_2019-01-07.xlsx": [
        "https://www.cnes-geipan.fr/sites/default/files/Description_des_tables_et_champs_de_donn%C3%A9es_de_la_base_du_geipan_2019-01-07.xlsx",
        "https://www.geipan.fr/sites/default/files/Description_des_tables_et_champs_de_donn%C3%A9es_de_la_base_du_geipan_2019-01-07.xlsx",
        "https://geipan.fr/sites/default/files/Description_des_tables_et_champs_de_donn%C3%A9es_de_la_base_du_geipan_2019-01-07.xlsx",
    ],
    "GEIPAN_database_history_2019-02-26.pdf": [
        "https://www.cnes-geipan.fr/sites/default/files/2019-02-26_Historique_des_bases_au_GEIPAN.pdf",
        "https://www.geipan.fr/sites/default/files/2019-02-26_Historique_des_bases_au_GEIPAN.pdf",
        "https://geipan.fr/sites/default/files/2019-02-26_Historique_des_bases_au_GEIPAN.pdf",
    ],
    "GEIPAN_statistics_page.html": [
        "https://www.geipan.fr/fr/stats",
        "https://geipan.fr/fr/stats",
    ],
}

WORKBOOK_PARSE_ERROR: str | None = None
ORIGINAL_LOAD_WORKBOOK = base.load_workbook


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def request_bytes(url: str, timeout: int = 300) -> tuple[bytes, str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; Watchers-UFO-Atlas/1.0; public-source-preservation)",
            "Accept": "*/*",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
        if not data:
            raise RuntimeError("empty response")
        headers = "\n".join(f"{key}: {value}" for key, value in response.headers.items())
        return data, response.geturl(), headers


def archive_candidates(original_url: str) -> list[str]:
    query = urllib.parse.urlencode(
        {
            "url": original_url,
            "output": "json",
            "filter": "statuscode:200",
            "fl": "timestamp,original,statuscode,mimetype,digest,length",
            "limit": "-10",
        }
    )
    cdx_url = f"https://web.archive.org/cdx/search/cdx?{query}"
    try:
        data, _, _ = request_bytes(cdx_url, timeout=180)
        rows = json.loads(data.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(rows, list) or len(rows) < 2:
        return []
    captures: list[str] = []
    for row in reversed(rows[1:]):
        if not isinstance(row, list) or len(row) < 2:
            continue
        timestamp, archived_original = str(row[0]), str(row[1])
        captures.append(f"https://web.archive.org/web/{timestamp}id_/{archived_original}")
    return captures


def resilient_download(_primary_url: str, destination: Path, headers_destination: Path) -> None:
    errors: list[str] = []
    direct = CANDIDATES[destination.name]

    for url in direct:
        for attempt in range(2):
            try:
                data, final_url, response_headers = request_bytes(url)
                destination.write_bytes(data)
                headers_destination.write_text(
                    f"acquisition_method: LIVE_OFFICIAL\nrequested_url: {url}\n"
                    f"final_url: {final_url}\n{response_headers}\n",
                    encoding="utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"LIVE {url}: {type(exc).__name__}: {exc}")
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    wait = min(45, int(retry_after)) if retry_after and retry_after.isdigit() else 12
                else:
                    wait = 4
                if attempt == 0:
                    time.sleep(wait)

    for original in direct:
        for archived_url in archive_candidates(original):
            try:
                data, final_url, response_headers = request_bytes(archived_url)
                destination.write_bytes(data)
                headers_destination.write_text(
                    f"acquisition_method: INTERNET_ARCHIVE_COPY_OF_OFFICIAL_SOURCE\n"
                    f"official_url: {original}\narchive_url: {archived_url}\n"
                    f"final_url: {final_url}\n{response_headers}\n",
                    encoding="utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"ARCHIVE {archived_url}: {type(exc).__name__}: {exc}")

    if destination.suffix.lower() == ".html":
        for original in direct:
            reader_url = "https://r.jina.ai/" + original
            try:
                data, final_url, response_headers = request_bytes(reader_url)
                destination.write_bytes(data)
                headers_destination.write_text(
                    f"acquisition_method: TEXT_READER_COPY_OF_OFFICIAL_PAGE\n"
                    f"official_url: {original}\nreader_url: {reader_url}\n"
                    f"final_url: {final_url}\n{response_headers}\n",
                    encoding="utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"READER {reader_url}: {type(exc).__name__}: {exc}")

    raise RuntimeError(
        f"Unable to acquire required GEIPAN source {destination.name}.\n" + "\n".join(errors)
    )


class EmptyWorkbook:
    """Minimal workbook facade used only when the official XLSX cannot parse."""

    worksheets: list[Any] = []


def resilient_load_workbook(*args: Any, **kwargs: Any) -> Any:
    global WORKBOOK_PARSE_ERROR
    try:
        return ORIGINAL_LOAD_WORKBOOK(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        WORKBOOK_PARSE_ERROR = f"{type(exc).__name__}: {exc}"
        return EmptyWorkbook()


def update_json(path: Path, updates: dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(updates)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def parse_live_stats(stats_html: Path) -> dict[str, Any]:
    text = stats_html.read_text(encoding="utf-8", errors="replace")
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
        "source_file": stats_html.name,
        "parse_status": "PARSED" if total_match else "COUNT_NOT_RECOVERED",
    }


def rebuild_release(root: Path) -> str:
    output = root / "output"
    release = root / "release"
    release_output = release / "output"
    release_output.mkdir(parents=True, exist_ok=True)
    for path in output.iterdir():
        if path.is_file():
            shutil.copy2(path, release_output / path.name)

    manifest_files = []
    for path in sorted(release.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            manifest_files.append(
                {
                    "path": str(path.relative_to(release)),
                    "bytes": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
    manifest = {
        "package_id": "UFO-ATLAS-GEIPAN-SOURCE-SNAPSHOT-0.1.0",
        "source_collection_id": base.COLLECTION_ID,
        "files": manifest_files,
        "manifest_note": "The manifest intentionally excludes its own self-hash.",
    }
    manifest_path = release_output / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    shutil.copy2(manifest_path, output / "manifest.json")

    package_name = "GEIPAN_OFFICIAL_SOURCE_SNAPSHOT_v0.1.0.zip"
    package = root / package_name
    package.unlink(missing_ok=True)
    with zipfile.ZipFile(package, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for path in sorted(release.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(release))
    with zipfile.ZipFile(package) as archive:
        bad = archive.testzip()
        if bad:
            raise RuntimeError(f"ZIP integrity failure at {bad}")
        nested = [name for name in archive.namelist() if name.lower().endswith(".zip")]
        if nested:
            raise RuntimeError(f"Nested ZIP files are prohibited: {nested}")
    package_hash = sha256(package)
    (root / f"{package_name}.sha256").write_text(
        f"{package_hash}  {package_name}\n",
        encoding="utf-8",
    )
    return package_hash


def postprocess(root: Path) -> None:
    output = root / "output"
    live_stats = parse_live_stats(root / "raw" / "GEIPAN_statistics_page.html")
    (output / "LIVE_STATS_CENSUS.json").write_text(
        json.dumps(live_stats, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    source_collection = json.loads((output / "SOURCE_COLLECTION.json").read_text(encoding="utf-8"))
    static_cases = int(source_collection["case_row_count"])
    live_cases = live_stats.get("published_case_count")
    gap = (live_cases - static_cases) if isinstance(live_cases, int) else None
    source_collection.update(
        {
            "live_published_case_count": live_cases,
            "live_statistics_date_original": live_stats.get("statistics_date_original"),
            "static_csv_case_gap_relative_to_live_site": gap,
            "source_scope_status": (
                "STATIC_DOWNLOAD_EXPORT_IS_OLDER_THAN_LIVE_PUBLISHED_DATABASE"
                if isinstance(gap, int) and gap > 0
                else "STATIC_EXPORT_RECONCILED_OR_LIVE_COUNT_UNAVAILABLE"
            ),
            "schema_workbook_parse_status": (
                "RAW_FILE_PRESERVED_PARSE_ERROR" if WORKBOOK_PARSE_ERROR else "PARSED"
            ),
            "schema_workbook_parse_error": WORKBOOK_PARSE_ERROR,
        }
    )
    (output / "SOURCE_COLLECTION.json").write_text(
        json.dumps(source_collection, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    census = update_json(
        output / "SOURCE_CENSUS.json",
        {
            "live_stats_census": live_stats,
            "static_csv_case_gap_relative_to_live_site": gap,
            "schema_workbook_parse_status": (
                "RAW_FILE_PRESERVED_PARSE_ERROR" if WORKBOOK_PARSE_ERROR else "PARSED"
            ),
            "schema_workbook_parse_error": WORKBOOK_PARSE_ERROR,
        },
    )

    profile_path = output / "GEIPAN_SCHEMA_WORKBOOK_PROFILE.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile.update(
        {
            "parse_status": "RAW_FILE_PRESERVED_PARSE_ERROR" if WORKBOOK_PARSE_ERROR else "PARSED",
            "parse_error": WORKBOOK_PARSE_ERROR,
            "raw_file_sha256": sha256(root / "raw" / "GEIPAN_table_field_description_2019-01-07.xlsx"),
            "recovery_note": (
                "The official workbook is preserved byte-for-byte. CSV headers remain sufficient to begin source-field mapping; "
                "the workbook will be reparsed from an alternate official or archived copy if available."
                if WORKBOOK_PARSE_ERROR
                else None
            ),
        }
    )
    profile_path.write_text(json.dumps(profile, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    validation_path = output / "VALIDATION_REPORT.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    limitations = list(validation.get("known_limitations", []))
    if WORKBOOK_PARSE_ERROR:
        limitations.append(
            "The official field-description XLSX has malformed ZIP offsets and could not be parsed by openpyxl; the raw file and all CSV headers are preserved."
        )
    if isinstance(gap, int) and gap > 0:
        limitations.append(
            f"The downloadable case CSV contains {static_cases} rows while the live GEIPAN statistics page reports {live_cases} published cases; live-site acquisition is required for the remaining {gap} cases."
        )
    validation.update(
        {
            "schema_workbook_parse_status": (
                "RAW_FILE_PRESERVED_PARSE_ERROR" if WORKBOOK_PARSE_ERROR else "PARSED"
            ),
            "schema_workbook_parse_error": WORKBOOK_PARSE_ERROR,
            "live_published_case_count": live_cases,
            "live_statistics_date_original": live_stats.get("statistics_date_original"),
            "static_csv_case_gap_relative_to_live_site": gap,
            "scope_completion_status": (
                "STATIC_EXPORT_ACQUIRED_LIVE_SITE_EXPANSION_REQUIRED"
                if isinstance(gap, int) and gap > 0
                else "STATIC_EXPORT_ACQUIRED"
            ),
            "known_limitations": limitations,
        }
    )
    validation_path.write_text(json.dumps(validation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    readme_path = output / "README_FIRST.md"
    readme = readme_path.read_text(encoding="utf-8")
    readme += (
        "\n## Live-site reconciliation\n\n"
        f"The official static case CSV contains **{static_cases:,}** case rows. "
        + (
            f"The live statistics page reports **{live_cases:,}** published cases as of **{live_stats.get('statistics_date_original')}**, "
            f"leaving **{gap:,}** published cases to acquire from the current site.\n"
            if isinstance(gap, int) and gap > 0
            else "A current live-site count could not be reconciled automatically.\n"
        )
        + "\nThe static export is therefore a preserved source snapshot, not the final GEIPAN corpus.\n"
    )
    if WORKBOOK_PARSE_ERROR:
        readme += (
            "\n## Official schema-workbook condition\n\n"
            "The downloaded 2019 XLSX is retained byte-for-byte, but its ZIP component offsets are malformed and openpyxl cannot parse it. "
            "This does not affect preservation of the two CSV exports. All 35 case columns and 263 testimony/observation columns are preserved directly from their headers.\n"
        )
    readme_path.write_text(readme, encoding="utf-8")

    package_hash = rebuild_release(root)
    final_validation = json.loads(validation_path.read_text(encoding="utf-8"))
    final_validation["package_sha256"] = package_hash
    validation_path.write_text(json.dumps(final_validation, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    # Sync the final validation and rebuild once more so the archive contains it.
    package_hash = rebuild_release(root)
    print(
        json.dumps(
            {
                "overall_status": final_validation["overall_status"],
                "source_record_count": final_validation["source_record_count"],
                "static_case_rows": static_cases,
                "live_published_cases": live_cases,
                "live_case_gap": gap,
                "schema_workbook_parse_status": final_validation["schema_workbook_parse_status"],
                "package_sha256": package_hash,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default="snapshot")
    parser.add_argument("--snapshot-date", default="2026-09-03")
    args = parser.parse_args()
    base.download = resilient_download
    base.load_workbook = resilient_load_workbook
    base.build(args)
    postprocess(Path(args.output_root))


if __name__ == "__main__":
    main()
