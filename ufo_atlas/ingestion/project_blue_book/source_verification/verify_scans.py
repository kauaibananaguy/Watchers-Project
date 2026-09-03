#!/usr/bin/env python3
"""Project Blue Book scan-manifest and text-layer verification.

This pipeline is source verification only. It produces source-neutral evidence
that can be merged into the central UFO Atlas. It does not create a Blue Book
project hierarchy and it does not use OCR. Image-only pages are remanded for
visual/manual verification rather than guessed.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

TREE_SHAS = {
    "1940s": "c7cc1ba868e3b5bed73ebeecba9fe7de7b345398",
    "1950s": "53caf1a404d506e191b726c9f7cfbe30dfe2dbf3",
    "1960s": "8b97483adba5616a120aa2cac4a8a2c248d6b10c",
    "19XXs": "520979a1bd3106a9e1c3e5020abf4676608cf599",
}
SCAN_REPO = "UAPMonitor/eksopolitiikka-ProjectBlueBook"
TEXT_REPO = "dansterdam/blue_book_scanner"
TEXT_ZIPS = {
    "1940s": "1940s_cases.zip",
    "1950s": "1950s_cases.zip",
    "1960s": "1960s_cases.zip",
    "19XXs": "19XXs_cases.zip",
}
FORM_LABELS = [
    "DATE OF SIGHTING", "DATE - TIME GROUP", "LOCATION", "SOURCE",
    "CONCLUSION", "NUMBER OF OBJECTS", "NO. IN GROUP",
    "LENGTH OF OBSERVATION", "DURATION", "TYPE OF OBSERVATION",
    "COURSE", "PHOTOS", "PHYSICAL EVIDENCE", "SHAPE", "COLOR",
    "SIZE", "ALTITUDE", "WEATHER", "REMARKS",
    "BRIEF SUMMARY AND ANALYSIS",
]
LABEL_RE = re.compile(
    r"(?im)^\s*(?:\d{1,2}\.\s*)?(" + "|".join(re.escape(x) for x in sorted(FORM_LABELS, key=len, reverse=True)) + r")\s*(?:[:\-]\s*(.*))?$"
)
PAGE_RE = re.compile(r"(?im)^\s*-\s*page\s+(\d+)\s*-\s*$")


def request_bytes(url: str, token: str | None = None, retries: int = 5) -> bytes:
    headers = {"User-Agent": "Watchers-UFO-Atlas-BlueBook-Verification/1.0"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=180) as response:
                return response.read()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < retries:
                time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"Unable to retrieve {url}: {last}")


def request_json(url: str, token: str | None = None) -> Any:
    return json.loads(request_bytes(url, token).decode("utf-8"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"\[?(?:redacted|illegible)\]?", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def similarity(a: str, b: str) -> float:
    a = normalized_text(a)
    b = normalized_text(b)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    # SequenceMatcher is bounded by trimming repeated boilerplate-heavy text.
    return round(SequenceMatcher(None, a[:250000], b[:250000], autojunk=True).ratio(), 6)


def split_derivative_pages(text: str) -> dict[int, str]:
    matches = list(PAGE_RE.finditer(text))
    if not matches:
        return {1: text}
    pages: dict[int, str] = {}
    pre = text[: matches[0].start()].strip()
    if pre:
        pages[0] = pre
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        pages[int(match.group(1))] = text[start:end].strip()
    return pages


def extract_form_values(text: str) -> dict[str, list[str]]:
    lines = text.splitlines()
    found: dict[str, list[str]] = {}
    for index, line in enumerate(lines):
        match = LABEL_RE.match(line)
        if not match:
            continue
        label = re.sub(r"\s+", " ", match.group(1).upper()).strip()
        value = (match.group(2) or "").strip(" :-\t")
        if not value:
            for next_index in range(index + 1, min(index + 5, len(lines))):
                candidate = lines[next_index].strip()
                if not candidate:
                    continue
                if LABEL_RE.match(candidate):
                    break
                value = candidate.strip(" :-\t")
                break
        if value:
            found.setdefault(label, []).append(re.sub(r"\s+", " ", value)[:4000])
    return found


def get_revision(token: str | None) -> str:
    data = request_json(f"https://api.github.com/repos/{SCAN_REPO}/commits/main", token)
    return str(data["sha"])


def get_entries(decade: str, token: str | None) -> list[dict[str, Any]]:
    sha = TREE_SHAS[decade]
    data = request_json(f"https://api.github.com/repos/{SCAN_REPO}/git/trees/{sha}?recursive=1", token)
    if data.get("truncated"):
        raise RuntimeError(f"Git tree response was truncated for {decade}")
    entries = [
        item for item in data.get("tree", [])
        if item.get("type") == "blob" and str(item.get("path", "")).lower().endswith(".pdf")
    ]
    return sorted(entries, key=lambda item: str(item["path"]).lower())


def make_batches(entries: list[dict[str, Any]], target_bytes: int) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    total = 0
    for item in entries:
        size = int(item.get("size") or 0)
        if current and total + size > target_bytes:
            batches.append(current)
            current = []
            total = 0
        current.append(item)
        total += size
    if current:
        batches.append(current)
    return batches


def command_plan(args: argparse.Namespace) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    revision = get_revision(token)
    include: list[dict[str, Any]] = []
    census: dict[str, Any] = {"scan_revision": revision, "decades": {}}
    for decade in TREE_SHAS:
        entries = get_entries(decade, token)
        batches = make_batches(entries, args.target_bytes)
        census["decades"][decade] = {
            "files": len(entries),
            "bytes": sum(int(item.get("size") or 0) for item in entries),
            "batches": len(batches),
        }
        for batch_index, batch in enumerate(batches):
            include.append({
                "decade": decade,
                "batch_index": batch_index,
                "target_bytes": args.target_bytes,
                "expected_files": len(batch),
                "expected_bytes": sum(int(item.get("size") or 0) for item in batch),
                "scan_revision": revision,
            })
    matrix = {"include": include}
    Path(args.output).write_text(json.dumps(matrix, separators=(",", ":")), encoding="utf-8")
    Path(args.census).write_text(json.dumps(census, indent=2), encoding="utf-8")
    print(json.dumps(matrix, separators=(",", ":")))


def load_text_archive(decade: str, token: str | None, target: Path) -> dict[str, str]:
    name = TEXT_ZIPS[decade]
    url = f"https://raw.githubusercontent.com/{TEXT_REPO}/main/data/scanned_casefiles/{name}"
    data = request_bytes(url, token)
    target.write_bytes(data)
    output: dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir() or not info.filename.lower().endswith(".txt"):
                continue
            text = archive.read(info).decode("utf-8", errors="replace")
            output[Path(info.filename).stem.lower()] = text
    return output


def classify_text_layer(pdf_pages: list[str], derivative: str) -> tuple[str, float]:
    combined = "\n".join(pdf_pages)
    if not normalized_text(combined):
        return "IMAGE_ONLY_REQUIRES_VISUAL_OR_OCR", 0.0
    score = similarity(combined, derivative)
    if score >= 0.70:
        return "TEXT_LAYER_STRONG_MATCH", score
    if score >= 0.35:
        return "TEXT_LAYER_PARTIAL_MATCH", score
    return "TEXT_LAYER_LOW_MATCH", score


def process_pdf(item: dict[str, Any], decade: str, revision: str, derivative: str, token: str | None, temp: Path) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    from pypdf import PdfReader

    path = str(item["path"])
    raw_url = f"https://raw.githubusercontent.com/{SCAN_REPO}/{revision}/{decade}/{path}"
    result: dict[str, Any] = {
        "decade": decade,
        "path": f"{decade}/{path}",
        "filename": Path(path).name,
        "scan_revision": revision,
        "git_blob_sha": item.get("sha"),
        "git_reported_size": int(item.get("size") or 0),
        "raw_url": raw_url,
        "derivative_found": bool(derivative),
        "download_status": "PENDING",
        "pdf_status": "PENDING",
        "verification_status": "PENDING",
        "error": None,
    }
    page_rows: list[dict[str, Any]] = []
    field_rows: list[dict[str, Any]] = []
    pdf_path = temp / Path(path).name
    try:
        data = request_bytes(raw_url, token)
        pdf_path.write_bytes(data)
        result.update({
            "download_status": "DOWNLOADED",
            "downloaded_size": len(data),
            "pdf_sha256": sha256_bytes(data),
        })
        reader = PdfReader(str(pdf_path), strict=False)
        pdf_pages: list[str] = []
        derivative_pages = split_derivative_pages(derivative)
        for page_index, page in enumerate(reader.pages, start=1):
            try:
                text = page.extract_text() or ""
                page_error = None
            except Exception as exc:  # noqa: BLE001
                text = ""
                page_error = repr(exc)
            pdf_pages.append(text)
            derivative_page = derivative_pages.get(page_index, "")
            page_rows.append({
                "path": result["path"],
                "page_number": page_index,
                "pdf_text_length": len(text),
                "pdf_text_sha256": sha256_bytes(text.encode("utf-8")),
                "derivative_text_length": len(derivative_page),
                "derivative_text_sha256": sha256_bytes(derivative_page.encode("utf-8")),
                "normalized_similarity": similarity(text, derivative_page),
                "pdf_text_status": "TEXT_PRESENT" if normalized_text(text) else "NO_TEXT_LAYER",
                "page_error": page_error,
            })
            pdf_fields = extract_form_values(text)
            derivative_fields = extract_form_values(derivative_page)
            for label in sorted(set(pdf_fields) | set(derivative_fields)):
                pdf_value = " | ".join(pdf_fields.get(label, []))
                derivative_value = " | ".join(derivative_fields.get(label, []))
                field_rows.append({
                    "path": result["path"],
                    "page_number": page_index,
                    "field_label": label,
                    "pdf_value": pdf_value,
                    "derivative_value": derivative_value,
                    "value_similarity": similarity(pdf_value, derivative_value),
                    "comparison_status": (
                        "MATCH" if pdf_value and derivative_value and similarity(pdf_value, derivative_value) >= 0.75
                        else "PDF_ONLY" if pdf_value and not derivative_value
                        else "DERIVATIVE_ONLY" if derivative_value and not pdf_value
                        else "CONFLICT_OR_LOW_MATCH"
                    ),
                })
        status, score = classify_text_layer(pdf_pages, derivative)
        result.update({
            "pdf_status": "READABLE",
            "page_count": len(reader.pages),
            "pages_with_text": sum(1 for text in pdf_pages if normalized_text(text)),
            "extracted_text_length": sum(len(text) for text in pdf_pages),
            "derivative_text_length": len(derivative),
            "document_similarity": score,
            "verification_status": status,
        })
    except Exception as exc:  # noqa: BLE001
        result.update({
            "pdf_status": "ERROR",
            "verification_status": "DOWNLOAD_OR_PDF_READ_ERROR",
            "error": repr(exc),
        })
    finally:
        pdf_path.unlink(missing_ok=True)
    return result, page_rows, field_rows


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def command_verify(args: argparse.Namespace) -> None:
    token = os.environ.get("GITHUB_TOKEN")
    revision = args.scan_revision or get_revision(token)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    entries = get_entries(args.decade, token)
    batches = make_batches(entries, args.target_bytes)
    if args.batch_index < 0 or args.batch_index >= len(batches):
        raise SystemExit(f"Batch {args.batch_index} outside 0..{len(batches)-1}")
    batch = batches[args.batch_index]
    text_archive = output / f"{args.decade}_text.zip"
    derivative = load_text_archive(args.decade, token, text_archive)
    text_archive.unlink(missing_ok=True)
    temp = output / "temp"
    temp.mkdir(exist_ok=True)
    results: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    for index, item in enumerate(batch, start=1):
        stem = Path(str(item["path"])).stem.lower()
        result, page_rows, field_rows = process_pdf(item, args.decade, revision, derivative.get(stem, ""), token, temp)
        results.append(result)
        pages.extend(page_rows)
        fields.extend(field_rows)
        print(f"[{args.decade} batch {args.batch_index}] {index}/{len(batch)} {item['path']} {result['verification_status']}", flush=True)
    temp.rmdir()
    write_jsonl(output / "documents.jsonl", results)
    write_jsonl(output / "pages.jsonl", pages)
    write_jsonl(output / "field_comparisons.jsonl", fields)
    summary = {
        "decade": args.decade,
        "batch_index": args.batch_index,
        "scan_revision": revision,
        "files": len(results),
        "bytes": sum(int(row.get("downloaded_size") or 0) for row in results),
        "status_counts": dict(Counter(row["verification_status"] for row in results)),
        "page_rows": len(pages),
        "field_comparison_rows": len(fields),
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def command_aggregate(args: argparse.Namespace) -> None:
    source = Path(args.input_dir)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    documents: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    fields: list[dict[str, Any]] = []
    for path in source.rglob("documents.jsonl"):
        documents.extend(read_jsonl(path))
    for path in source.rglob("pages.jsonl"):
        pages.extend(read_jsonl(path))
    for path in source.rglob("field_comparisons.jsonl"):
        fields.extend(read_jsonl(path))
    documents.sort(key=lambda row: row["path"])
    pages.sort(key=lambda row: (row["path"], int(row["page_number"])))
    fields.sort(key=lambda row: (row["path"], int(row["page_number"]), row["field_label"]))
    db = output / "PROJECT_BLUE_BOOK_SCAN_VERIFICATION_RESULTS.sqlite"
    if db.exists():
        db.unlink()
    connection = sqlite3.connect(db)
    connection.executescript("""
    PRAGMA foreign_keys=ON;
    CREATE TABLE documents(
      path TEXT PRIMARY KEY, decade TEXT NOT NULL, filename TEXT NOT NULL,
      scan_revision TEXT NOT NULL, git_blob_sha TEXT, git_reported_size INTEGER,
      raw_url TEXT NOT NULL, derivative_found INTEGER NOT NULL,
      download_status TEXT NOT NULL, downloaded_size INTEGER, pdf_sha256 TEXT,
      pdf_status TEXT NOT NULL, page_count INTEGER, pages_with_text INTEGER,
      extracted_text_length INTEGER, derivative_text_length INTEGER,
      document_similarity REAL, verification_status TEXT NOT NULL, error TEXT
    );
    CREATE TABLE pages(
      path TEXT NOT NULL REFERENCES documents(path), page_number INTEGER NOT NULL,
      pdf_text_length INTEGER NOT NULL, pdf_text_sha256 TEXT NOT NULL,
      derivative_text_length INTEGER NOT NULL, derivative_text_sha256 TEXT NOT NULL,
      normalized_similarity REAL NOT NULL, pdf_text_status TEXT NOT NULL,
      page_error TEXT, PRIMARY KEY(path,page_number)
    );
    CREATE TABLE field_comparisons(
      path TEXT NOT NULL REFERENCES documents(path), page_number INTEGER NOT NULL,
      field_label TEXT NOT NULL, pdf_value TEXT, derivative_value TEXT,
      value_similarity REAL NOT NULL, comparison_status TEXT NOT NULL,
      PRIMARY KEY(path,page_number,field_label,pdf_value,derivative_value)
    );
    CREATE INDEX idx_documents_status ON documents(verification_status);
    CREATE INDEX idx_pages_status ON pages(pdf_text_status);
    CREATE INDEX idx_fields_status ON field_comparisons(comparison_status);
    """)
    document_columns = [
        "path", "decade", "filename", "scan_revision", "git_blob_sha",
        "git_reported_size", "raw_url", "derivative_found", "download_status",
        "downloaded_size", "pdf_sha256", "pdf_status", "page_count",
        "pages_with_text", "extracted_text_length", "derivative_text_length",
        "document_similarity", "verification_status", "error",
    ]
    connection.executemany(
        f"INSERT INTO documents VALUES({','.join('?' for _ in document_columns)})",
        [tuple(int(row.get(column)) if column == "derivative_found" else row.get(column) for column in document_columns) for row in documents],
    )
    page_columns = [
        "path", "page_number", "pdf_text_length", "pdf_text_sha256",
        "derivative_text_length", "derivative_text_sha256", "normalized_similarity",
        "pdf_text_status", "page_error",
    ]
    connection.executemany(
        f"INSERT INTO pages VALUES({','.join('?' for _ in page_columns)})",
        [tuple(row.get(column) for column in page_columns) for row in pages],
    )
    field_columns = [
        "path", "page_number", "field_label", "pdf_value", "derivative_value",
        "value_similarity", "comparison_status",
    ]
    connection.executemany(
        f"INSERT OR IGNORE INTO field_comparisons VALUES({','.join('?' for _ in field_columns)})",
        [tuple(row.get(column) for column in field_columns) for row in fields],
    )
    connection.commit()
    quick = connection.execute("PRAGMA quick_check").fetchone()[0]
    foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    connection.close()
    status_counts = Counter(row["verification_status"] for row in documents)
    summary = {
        "documents": len(documents),
        "pages": len(pages),
        "field_comparisons": len(fields),
        "downloaded_bytes": sum(int(row.get("downloaded_size") or 0) for row in documents),
        "verification_status_counts": dict(status_counts),
        "sqlite_quick_check": quick,
        "foreign_key_violations": foreign_keys,
        "source_policy": "Original scan controls. No OCR was used; image-only pages remain explicitly unverified.",
    }
    (output / "SUMMARY.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    write_jsonl(output / "documents.jsonl", documents)
    with (output / "documents.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=document_columns)
        writer.writeheader()
        writer.writerows({column: row.get(column) for column in document_columns} for row in documents)
    checksums = []
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    (output / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    if quick != "ok" or foreign_keys:
        raise SystemExit(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--target-bytes", type=int, default=600_000_000)
    plan.add_argument("--output", default="matrix.json")
    plan.add_argument("--census", default="scan_census.json")
    plan.set_defaults(function=command_plan)
    verify = commands.add_parser("verify")
    verify.add_argument("--decade", required=True, choices=sorted(TREE_SHAS))
    verify.add_argument("--batch-index", type=int, required=True)
    verify.add_argument("--target-bytes", type=int, default=600_000_000)
    verify.add_argument("--scan-revision")
    verify.add_argument("--output-dir", required=True)
    verify.set_defaults(function=command_verify)
    aggregate = commands.add_parser("aggregate")
    aggregate.add_argument("--input-dir", required=True)
    aggregate.add_argument("--output-dir", required=True)
    aggregate.set_defaults(function=command_aggregate)
    return root


def main() -> None:
    args = parser().parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
