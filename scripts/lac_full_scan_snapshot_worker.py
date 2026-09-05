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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import requests
from PIL import Image

CHUNK = int(os.environ["CHUNK"])
CHUNK_COUNT = int(os.environ.get("CHUNK_COUNT", "8"))
INVENTORY_ROOT = Path(os.environ.get("INVENTORY_ROOT", "navigation"))
INDEXED_OCR_ROOT = Path(os.environ.get("INDEXED_OCR_ROOT", "indexed_ocr"))
OUTPUT = Path(os.environ.get("OUTPUT", "output"))
OUTPUT.mkdir(parents=True, exist_ok=True)

USER_AGENT = "Mozilla/5.0 (compatible; WatchersProject-UFOAtlas/1.0; archival research)"
_thread_local = threading.local()


def get_session() -> requests.Session:
    if not hasattr(_thread_local, "session"):
        session = requests.Session()
        session.headers.update({"User-Agent": USER_AGENT})
        _thread_local.session = session
    return _thread_local.session


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def request_bytes(url: str, timeout: int = 120) -> tuple[bytes, str, int]:
    last_error: Exception | None = None
    for attempt in range(7):
        try:
            response = get_session().get(url, timeout=timeout, allow_redirects=True)
            if response.status_code == 404:
                raise FileNotFoundError(f"HTTP 404: {url}")
            response.raise_for_status()
            return response.content, response.headers.get("content-type", ""), response.status_code
        except FileNotFoundError:
            raise
        except Exception as exc:  # pragma: no cover - network retry path
            last_error = exc
            time.sleep(min(12.0, 1.5 * (attempt + 1)))
    raise RuntimeError(f"Unable to download {url}: {last_error}")


def score_text(text: str) -> int:
    return len(text.strip()) + 3 * sum(ch.isalpha() for ch in text) + sum(ch.isdigit() for ch in text)


def run_ocr(image_path: Path) -> tuple[str, str, str]:
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
        process = subprocess.run(command, capture_output=True, timeout=120, check=False)
        text = process.stdout.decode("utf-8", errors="replace").replace("\x0c", "").strip()
        stderr = process.stderr.decode("utf-8", errors="replace")[-1000:]
        attempts.append((score_text(text), psm, text, stderr))
        if psm == "6" and len(text) > 350 and sum(ch.isalpha() for ch in text) > 180:
            break
    best = max(attempts, key=lambda value: value[0])
    return best[2], f"tesseract-eng+fra-oem1-psm{best[1]}", best[3]


def load_navigation_inventory() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for path in sorted(INVENTORY_ROOT.rglob("LAC_SCAN_NAVIGATION_INVENTORY_CHUNK_*.csv")):
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows.extend(csv.DictReader(handle))
    if not rows:
        raise FileNotFoundError("No navigation inventory CSV files were found")
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for row in rows:
        key = (row["isn_id_nbr"], row["page_id_nbr"])
        if key in unique:
            raise RuntimeError(f"Duplicate navigation inventory key: {key}")
        unique[key] = row
    return sorted(unique.values(), key=lambda row: (int(row["isn_id_nbr"]), int(row["source_file_sequence"])))


def load_existing_ocr() -> dict[tuple[str, str], dict]:
    index: dict[tuple[str, str], dict] = {}
    for path in sorted(INDEXED_OCR_ROOT.rglob("SCAN_OCR_CHUNK_*.jsonl")):
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("status") != "COMPLETE":
                    continue
                key = (str(row["isn_id_nbr"]), str(row["page_id_nbr"]))
                index[key] = row
    return index


navigation_rows = load_navigation_inventory()
existing_ocr = load_existing_ocr()
selected = [
    row
    for row in navigation_rows
    if (int(row["isn_id_nbr"]) + int(row["page_id_nbr"])) % CHUNK_COUNT == CHUNK
]
selected.sort(key=lambda row: (int(row["isn_id_nbr"]), int(row["source_file_sequence"])))


def process(row: dict[str, str]) -> dict:
    isn = row["isn_id_nbr"]
    page = row["page_id_nbr"]
    key = (isn, page)
    scan_id = f"LAC-SCAN-{isn}-{page}"
    url = row["direct_scan_url"]
    started = time.time()
    base = {
        "chunk": CHUNK,
        "scan_id": scan_id,
        "isn_id_nbr": isn,
        "page_id_nbr": page,
        "source_file_sequence": row["source_file_sequence"],
        "record_group": row.get("record_group", ""),
        "document_title": row.get("document_title", ""),
        "indexed_in_portal_metadata": int(row.get("indexed_in_portal_metadata", "0") or 0),
        "indexed_portal_record_ids": row.get("indexed_portal_record_ids", ""),
        "image_page_url": row.get("image_page_url", ""),
        "scan_url": url,
    }
    try:
        data, content_type, status_code = request_bytes(url)
        if len(data) < 1000:
            raise RuntimeError(f"Scan payload is unexpectedly small: {len(data)} bytes")
        suffix = Path(urllib.parse.urlparse(url).path).suffix.lower() or ".jpg"
        relative_path = Path("scans") / isn / f"{page}{suffix}"
        destination = OUTPUT / relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        with Image.open(destination) as image:
            width, height, mode, image_format = image.width, image.height, image.mode, image.format
        prior = existing_ocr.get(key)
        if prior:
            ocr_text = prior.get("ocr_text", "")
            ocr_method = prior.get("ocr_method", "existing-indexed-ocr")
            ocr_stderr = prior.get("ocr_stderr", "")
            ocr_origin = "REUSED_VERIFIED_INDEXED_OCR"
        else:
            ocr_text, ocr_method, ocr_stderr = run_ocr(destination)
            ocr_origin = "NEW_OCR_FOR_UNINDEXED_PHYSICAL_PAGE"
        return {
            **base,
            "status": "COMPLETE",
            "http_status": status_code,
            "stored_relative_path": str(relative_path),
            "scan_sha256": sha256_bytes(data),
            "scan_bytes": len(data),
            "content_type": content_type,
            "image_width": width,
            "image_height": height,
            "image_mode": mode,
            "image_format": image_format,
            "ocr_origin": ocr_origin,
            "ocr_method": ocr_method,
            "ocr_text": ocr_text,
            "ocr_text_sha256": sha256_bytes(ocr_text.encode("utf-8")),
            "ocr_characters": len(ocr_text),
            "ocr_error": "",
            "ocr_stderr": ocr_stderr,
            "elapsed_seconds": round(time.time() - started, 3),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        }
    except FileNotFoundError as exc:
        return {
            **base,
            "status": "SOURCE_SCAN_UNAVAILABLE_HTTP_404",
            "http_status": 404,
            "stored_relative_path": "",
            "scan_sha256": "",
            "scan_bytes": 0,
            "content_type": "",
            "image_width": "",
            "image_height": "",
            "image_mode": "",
            "image_format": "",
            "ocr_origin": "",
            "ocr_method": "",
            "ocr_text": "",
            "ocr_text_sha256": "",
            "ocr_characters": 0,
            "ocr_error": str(exc),
            "ocr_stderr": "",
            "elapsed_seconds": round(time.time() - started, 3),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as exc:
        return {
            **base,
            "status": "ERROR",
            "http_status": "",
            "stored_relative_path": "",
            "scan_sha256": "",
            "scan_bytes": 0,
            "content_type": "",
            "image_width": "",
            "image_height": "",
            "image_mode": "",
            "image_format": "",
            "ocr_origin": "",
            "ocr_method": "",
            "ocr_text": "",
            "ocr_text_sha256": "",
            "ocr_characters": 0,
            "ocr_error": repr(exc),
            "ocr_stderr": "",
            "elapsed_seconds": round(time.time() - started, 3),
            "acquired_at": datetime.now(timezone.utc).isoformat(),
        }


results: list[dict] = []
with ThreadPoolExecutor(max_workers=4) as pool:
    futures = [pool.submit(process, row) for row in selected]
    for completed, future in enumerate(as_completed(futures), 1):
        results.append(future.result())
        if completed == 1 or completed % 50 == 0 or completed == len(futures):
            complete = sum(row["status"] == "COMPLETE" for row in results)
            unavailable = sum(row["status"] == "SOURCE_SCAN_UNAVAILABLE_HTTP_404" for row in results)
            errors = sum(row["status"] == "ERROR" for row in results)
            print(
                f"chunk {CHUNK}: {completed}/{len(futures)}; complete={complete}; unavailable={unavailable}; errors={errors}",
                flush=True,
            )

results.sort(key=lambda row: (int(row["isn_id_nbr"]), int(row["source_file_sequence"])))
if not results:
    raise RuntimeError(f"No physical scan pages selected for chunk {CHUNK}")

csv_fields = [key for key in results[0] if key != "ocr_text"]
with (OUTPUT / f"SOURCE_SCAN_LEDGER_CHUNK_{CHUNK:02d}.csv").open(
    "w", encoding="utf-8-sig", newline=""
) as handle:
    writer = csv.DictWriter(handle, fieldnames=csv_fields)
    writer.writeheader()
    writer.writerows({key: row.get(key, "") for key in csv_fields} for row in results)

with (OUTPUT / f"SCAN_CONTENT_CHUNK_{CHUNK:02d}.jsonl").open("w", encoding="utf-8") as handle:
    for row in results:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")

unexpected_errors = [row for row in results if row["status"] == "ERROR"]
unavailable = [row for row in results if row["status"] == "SOURCE_SCAN_UNAVAILABLE_HTTP_404"]
report = {
    "chunk": CHUNK,
    "chunk_count": CHUNK_COUNT,
    "physical_pages_selected": len(results),
    "complete_scan_snapshots": sum(row["status"] == "COMPLETE" for row in results),
    "source_scan_unavailable_http_404": len(unavailable),
    "unexpected_errors": len(unexpected_errors),
    "indexed_ocr_reused": sum(row.get("ocr_origin") == "REUSED_VERIFIED_INDEXED_OCR" for row in results),
    "unindexed_pages_newly_ocrd": sum(row.get("ocr_origin") == "NEW_OCR_FOR_UNINDEXED_PHYSICAL_PAGE" for row in results),
    "scan_bytes_total": sum(int(row.get("scan_bytes") or 0) for row in results),
    "ocr_characters_total": sum(int(row.get("ocr_characters") or 0) for row in results),
    "unavailable_scan_ids": [row["scan_id"] for row in unavailable],
    "errors": [{"scan_id": row["scan_id"], "error": row["ocr_error"]} for row in unexpected_errors],
    "status": "PASS_WITH_SOURCE_404S" if unavailable and not unexpected_errors else ("PASS" if not unexpected_errors else "FAIL"),
}
(OUTPUT / f"SOURCE_SCAN_SNAPSHOT_REPORT_CHUNK_{CHUNK:02d}.json").write_text(
    json.dumps(report, indent=2), encoding="utf-8"
)
print(json.dumps(report, indent=2), flush=True)
if unexpected_errors:
    raise SystemExit(f"{len(unexpected_errors)} unexpected scan acquisition/OCR errors in chunk {CHUNK}")
