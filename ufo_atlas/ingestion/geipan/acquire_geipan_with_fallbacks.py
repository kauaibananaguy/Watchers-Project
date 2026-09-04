#!/usr/bin/env python3
"""Run the GEIPAN acquisition builder with official-host and archive fallbacks."""
from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

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
                    f"acquisition_method: LIVE_OFFICIAL\nrequested_url: {url}\nfinal_url: {final_url}\n{response_headers}\n",
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

    # The official site can rate-limit cloud runners. Preserve a public archived
    # copy only when every live official hostname has failed.
    for original in direct:
        for archived_url in archive_candidates(original):
            try:
                data, final_url, response_headers = request_bytes(archived_url)
                destination.write_bytes(data)
                headers_destination.write_text(
                    f"acquisition_method: INTERNET_ARCHIVE_COPY_OF_OFFICIAL_SOURCE\n"
                    f"official_url: {original}\narchive_url: {archived_url}\nfinal_url: {final_url}\n"
                    f"{response_headers}\n",
                    encoding="utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"ARCHIVE {archived_url}: {type(exc).__name__}: {exc}")

    # Jina Reader is acceptable only for the two informational HTML pages. It
    # is never used for the database CSVs, workbook, or PDF.
    if destination.suffix.lower() == ".html":
        for original in direct:
            reader_url = "https://r.jina.ai/" + original
            try:
                data, final_url, response_headers = request_bytes(reader_url)
                destination.write_bytes(data)
                headers_destination.write_text(
                    f"acquisition_method: TEXT_READER_COPY_OF_OFFICIAL_PAGE\n"
                    f"official_url: {original}\nreader_url: {reader_url}\nfinal_url: {final_url}\n"
                    f"{response_headers}\n",
                    encoding="utf-8",
                )
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"READER {reader_url}: {type(exc).__name__}: {exc}")

    raise RuntimeError(
        f"Unable to acquire required GEIPAN source {destination.name}.\n" + "\n".join(errors)
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-root", default="snapshot")
    parser.add_argument("--snapshot-date", default="2026-09-03")
    args = parser.parse_args()
    base.download = resilient_download
    base.build(args)


if __name__ == "__main__":
    main()
