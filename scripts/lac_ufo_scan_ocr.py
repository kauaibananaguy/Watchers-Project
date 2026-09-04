#!/usr/bin/env python3
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from PIL import Image

CHUNK = int(os.environ.get("CHUNK", "0"))
CHUNK_COUNT = int(os.environ.get("CHUNK_COUNT", "8"))
WORKERS = int(os.environ.get("WORKERS", "2"))
SOURCE_CSV = next(Path(os.environ.get("METADATA_DIR", "metadata")).rglob("LAC_UFO_SOURCE_INDEX.csv"))
OUT = Path(os.environ.get("OUTPUT_DIR", "output"))
OUT.mkdir(parents=True, exist_ok=True)
UA = "Mozilla/5.0 (compatible; WatchersProject-UFOAtlas/1.0; archival research)"
LOCAL = threading.local()


def get_session() -> requests.Session:
    if not hasattr(LOCAL, "session"):
        session = requests.Session()
        session.headers.update({"User-Agent": UA})
        LOCAL.session = session
    return LOCAL.session


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request(url: str, timeout: int = 90) -> requests.Response:
    last: Exception | None = None
    for attempt in range(7):
        try:
            response = get_session().get(url, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            return response
        except Exception as exc:
            last = exc
            time.sleep(min(18.0, 1.75 * (attempt + 1)))
    raise RuntimeError(f"{url}: {last}")


def direct_scan_url(image_page_url: str) -> tuple[str, str, int]:
    response = request(image_page_url, 90)
    html = response.content.decode("latin-1", errors="replace")
    match = re.search(
        r"https?://data2\.collectionscanada\.gc\.ca/[^\"'<>\s]+?\.(?:jpe?g|png|tiff?)",
        html,
        re.I,
    )
    direct = match.group(0) if match else ""
    if not direct:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup.find_all("img", src=True):
            candidate = urllib.parse.urljoin(image_page_url, tag["src"])
            if "data2.collectionscanada.gc.ca" in candidate and re.search(
                r"\.(?:jpe?g|png|tiff?)$", urllib.parse.urlparse(candidate).path, re.I
            ):
                direct = candidate
                break
    if not direct:
        raise RuntimeError("No direct LAC scan URL found on image-view page")
    direct = direct.replace("&amp;", "&")
    if direct.startswith("http://"):
        direct = "https://" + direct[len("http://") :]
    return direct, sha256_bytes(response.content), len(response.content)


def ocr_scan(data: bytes, suffix: str) -> tuple[str, str, str]:
    with tempfile.TemporaryDirectory(prefix="lacocr_") as temp_dir:
        image_path = Path(temp_dir) / ("scan" + suffix)
        image_path.write_bytes(data)
        attempts: list[tuple[int, str, str, str]] = []
        for psm in ("6", "3"):
            command = [
                "tesseract",
                str(image_path),
                "stdout",
                "-l",
                "eng+fra",
                "--oem",
                "1",
                "--psm",
                psm,
                "-c",
                "preserve_interword_spaces=1",
            ]
            process = subprocess.run(command, capture_output=True, timeout=90, check=False)
            text = process.stdout.decode("utf-8", errors="replace").replace("\x0c", "").strip()
            score = len(text) + 3 * sum(char.isalpha() for char in text)
            stderr = process.stderr.decode("utf-8", errors="replace")[-1000:]
            attempts.append((score, psm, text, stderr))
            if psm == "6" and len(text) >= 120 and sum(char.isalpha() for char in text) >= 60:
                break
        best = max(attempts, key=lambda item: item[0])
        return best[2], f"tesseract-eng+fra-oem1-psm{best[1]}", best[3]


def process_scan(item: tuple[tuple[str, str], list[dict[str, str]]]) -> dict[str, object]:
    (isn, page), members = item
    representative = members[0]
    image_page_url = representative["image_view_url"]
    scan_id = f"LAC-SCAN-{isn}-{page}"
    started = time.time()
    base: dict[str, object] = {
        "chunk": CHUNK,
        "scan_id": scan_id,
        "isn_id_nbr": isn,
        "page_id_nbr": page,
        "representative_portal_record_id": representative["portal_record_id"],
        "portal_record_ids": " | ".join(row["portal_record_id"] for row in members),
        "source_record_ids": " | ".join(row["source_record_id"] for row in members),
        "source_sequences": " | ".join(row["source_sequence"] for row in members),
        "source_record_count": len(members),
        "image_page_url": image_page_url,
    }
    try:
        scan_url, page_sha, page_bytes = direct_scan_url(image_page_url)
        response = request(scan_url, 120)
        data = response.content
        if len(data) < 1000:
            raise RuntimeError(f"Scan payload too small: {len(data)} bytes")
        suffix = Path(urllib.parse.urlparse(scan_url).path).suffix.lower() or ".jpg"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temp:
            temp.write(data)
            temp_path = Path(temp.name)
        try:
            with Image.open(temp_path) as image:
                width, height, mode, image_format = image.width, image.height, image.mode, image.format
        finally:
            temp_path.unlink(missing_ok=True)
        text, method, stderr = ocr_scan(data, suffix)
        return {
            **base,
            "status": "COMPLETE",
            "scan_url": scan_url,
            "image_page_sha256": page_sha,
            "image_page_bytes": page_bytes,
            "scan_sha256": sha256_bytes(data),
            "scan_bytes": len(data),
            "content_type": response.headers.get("content-type", ""),
            "image_width": width,
            "image_height": height,
            "image_mode": mode,
            "image_format": image_format,
            "ocr_method": method,
            "ocr_text": text,
            "ocr_text_sha256": sha256_bytes(text.encode("utf-8")),
            "ocr_characters": len(text),
            "ocr_error": "",
            "ocr_stderr": stderr,
            "elapsed_seconds": round(time.time() - started, 3),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {
            **base,
            "status": "ERROR",
            "scan_url": "",
            "image_page_sha256": "",
            "image_page_bytes": "",
            "scan_sha256": "",
            "scan_bytes": "",
            "content_type": "",
            "image_width": "",
            "image_height": "",
            "image_mode": "",
            "image_format": "",
            "ocr_method": "",
            "ocr_text": "",
            "ocr_text_sha256": "",
            "ocr_characters": 0,
            "ocr_error": repr(exc),
            "ocr_stderr": "",
            "elapsed_seconds": round(time.time() - started, 3),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        }


with SOURCE_CSV.open("r", encoding="utf-8-sig", newline="") as handle:
    rows = list(csv.DictReader(handle))

scan_to_rows: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
unresolved: list[dict[str, str]] = []
for row in rows:
    isn = (row.get("isn_id_nbr") or "").strip()
    page = (row.get("page_id_nbr") or "").strip()
    if isn.isdigit() and page.isdigit():
        scan_to_rows[(isn, page)].append(row)
    elif int(row["source_sequence"]) % CHUNK_COUNT == CHUNK:
        unresolved.append(row)

selected = [
    (key, sorted(members, key=lambda row: int(row["source_sequence"])))
    for key, members in scan_to_rows.items()
    if (int(key[0]) + int(key[1])) % CHUNK_COUNT == CHUNK
]
selected.sort(key=lambda item: min(int(row["source_sequence"]) for row in item[1]))

results: list[dict[str, object]] = []
with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    futures = [pool.submit(process_scan, item) for item in selected]
    for completed, future in enumerate(as_completed(futures), 1):
        results.append(future.result())
        if completed == 1 or completed % 50 == 0 or completed == len(selected):
            errors = sum(row["status"] != "COMPLETE" for row in results)
            print(f"chunk {CHUNK}: {completed}/{len(selected)} scans processed; errors={errors}", flush=True)
results.sort(key=lambda row: min(int(value) for value in str(row["source_sequences"]).split(" | ")))

if results:
    csv_fields = [field for field in results[0] if field != "ocr_text"]
    with (OUT / f"SCAN_LEDGER_CHUNK_{CHUNK:02d}.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        writer.writerows({field: row.get(field, "") for field in csv_fields} for row in results)
    with (OUT / f"SCAN_OCR_CHUNK_{CHUNK:02d}.jsonl").open("w", encoding="utf-8") as handle:
        for row in results:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

mapping: list[dict[str, object]] = []
for row in results:
    for source_id, portal_id, sequence in zip(
        str(row["source_record_ids"]).split(" | "),
        str(row["portal_record_ids"]).split(" | "),
        str(row["source_sequences"]).split(" | "),
    ):
        mapping.append(
            {
                "source_record_id": source_id,
                "portal_record_id": portal_id,
                "source_sequence": sequence,
                "scan_id": row["scan_id"],
                "scan_url": row["scan_url"],
                "scan_sha256": row["scan_sha256"],
                "ocr_text_sha256": row["ocr_text_sha256"],
                "status": row["status"],
                "notes": row["ocr_error"],
            }
        )
for row in unresolved:
    mapping.append(
        {
            "source_record_id": row["source_record_id"],
            "portal_record_id": row["portal_record_id"],
            "source_sequence": row["source_sequence"],
            "scan_id": "",
            "scan_url": "",
            "scan_sha256": "",
            "ocr_text_sha256": "",
            "status": "NO_SCAN_IDENTIFIER",
            "notes": "The official portal row contains no page_id_nbr; source record preserved for item-page disposition.",
        }
    )
mapping.sort(key=lambda row: int(str(row["source_sequence"])))
with (OUT / f"RECORD_SCAN_MAP_CHUNK_{CHUNK:02d}.csv").open("w", encoding="utf-8-sig", newline="") as handle:
    fields = ["source_record_id", "portal_record_id", "source_sequence", "scan_id", "scan_url", "scan_sha256", "ocr_text_sha256", "status", "notes"]
    writer = csv.DictWriter(handle, fieldnames=fields)
    writer.writeheader()
    writer.writerows(mapping)

errors = [row for row in results if row["status"] != "COMPLETE"]
report = {
    "chunk": CHUNK,
    "chunk_count": CHUNK_COUNT,
    "unique_scans": len(results),
    "source_records_mapped": len(mapping),
    "complete_scans": len(results) - len(errors),
    "error_scans": len(errors),
    "no_scan_identifier_records": len(unresolved),
    "scan_bytes_total": sum(int(row["scan_bytes"] or 0) for row in results),
    "ocr_characters_total": sum(int(row["ocr_characters"] or 0) for row in results),
    "errors": [{"scan_id": row["scan_id"], "error": row["ocr_error"]} for row in errors],
}
(OUT / f"CHUNK_REPORT_{CHUNK:02d}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
print(json.dumps(report, indent=2), flush=True)
