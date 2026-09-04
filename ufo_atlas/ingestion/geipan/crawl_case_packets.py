#!/usr/bin/env python3
"""Acquire official GEIPAN case pages and publicly linked case-packet assets.

This is a source-preservation and extraction pipeline for the one source-neutral
UFO Atlas. GEIPAN remains provenance; this script does not create a separate
public GEIPAN hierarchy or assign central Atlas identities.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

BASE = "https://www.geipan.fr"
USER_AGENT = "Watchers-UFO-Atlas/1.0 (public-source-preservation; low-rate crawler)"
ATTACHMENT_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff",
    ".mp3", ".wav", ".mp4", ".mov", ".avi", ".doc", ".docx", ".xls",
    ".xlsx", ".csv", ".txt", ".rtf", ".zip", ".kml", ".kmz",
}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", "" if value is None else str(value)).strip()


def normalize_url(url: str, base: str = BASE) -> str:
    url = clean(url).replace("&amp;", "&")
    if not url:
        return ""
    return urllib.parse.urljoin(base, url)


def request_bytes(url: str, retries: int = 9, timeout: int = 300) -> tuple[bytes, str, dict[str, str]]:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,application/pdf,image/*,*/*;q=0.8",
                    "Accept-Language": "fr,en;q=0.8",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                data = response.read()
                if not data:
                    raise RuntimeError("empty response")
                return data, response.geturl(), {str(k): str(v) for k, v in response.headers.items()}
        except Exception as exc:  # noqa: BLE001
            last = exc
            wait = min(300, 5 * (2**attempt))
            if isinstance(exc, urllib.error.HTTPError):
                if exc.code == 429:
                    retry = exc.headers.get("Retry-After") if exc.headers else None
                    wait = min(600, int(retry)) if retry and retry.isdigit() else min(600, 30 * (attempt + 1))
                elif 400 <= exc.code < 500 and exc.code not in (408, 429):
                    break
            if attempt + 1 < retries:
                time.sleep(wait)
    raise RuntimeError(f"Unable to fetch {url}: {type(last).__name__}: {last}")


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self.meta_description = ""
        self._in_title = False
        self._link_text: list[str] | None = None
        self._link_href = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        data = {k.lower(): (v or "") for k, v in attrs}
        if tag.lower() == "title":
            self._in_title = True
        if tag.lower() == "a" and data.get("href"):
            self._link_href = data["href"]
            self._link_text = []
        if tag.lower() == "meta" and data.get("name", "").lower() == "description":
            self.meta_description = clean(data.get("content"))

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
        if tag.lower() == "a" and self._link_text is not None:
            self.links.append((self._link_href, clean(" ".join(self._link_text))))
            self._link_text = None
            self._link_href = ""

    def handle_data(self, data: str) -> None:
        text = clean(data)
        if not text:
            return
        self.text_parts.append(text)
        if self._in_title:
            self.title_parts.append(text)
        if self._link_text is not None:
            self._link_text.append(text)


def parse_page(data: bytes, final_url: str) -> dict[str, Any]:
    encoding = "utf-8"
    head = data[:5000].decode("ascii", errors="ignore")
    match = re.search(r"charset=[\"']?([A-Za-z0-9_.-]+)", head, flags=re.I)
    if match:
        encoding = match.group(1)
    try:
        html = data.decode(encoding, errors="replace")
    except LookupError:
        encoding = "utf-8"
        html = data.decode("utf-8", errors="replace")
    parser = PageParser()
    parser.feed(html)
    text = clean("\n".join(parser.text_parts))
    title = clean(" ".join(parser.title_parts))
    links = []
    for href, label in parser.links:
        url = normalize_url(href, final_url)
        if url:
            links.append({"url": url, "label": label})
    # Preserve common label/value statements without claiming they are complete.
    fields = []
    label_patterns = [
        "Date d'observation", "Date de l'observation", "Département", "Commune",
        "Pays", "Classe", "Classification", "Résumé", "Synthèse", "Témoignage",
        "Nombre de témoins", "Durée", "Heure", "Étrangeté", "Consistance",
    ]
    for label in label_patterns:
        rx = re.compile(re.escape(label) + r"\s*[:\-]?\s*([^|]{1,500})", flags=re.I)
        m = rx.search(text)
        if m:
            value = clean(m.group(1))
            # Stop at a likely following label.
            for stop in label_patterns:
                if stop.lower() == label.lower():
                    continue
                pos = value.lower().find(stop.lower())
                if pos > 0:
                    value = value[:pos].strip()
            fields.append({"label": label, "value": value[:2000]})
    return {
        "encoding": encoding,
        "title": title,
        "meta_description": parser.meta_description,
        "text": text,
        "links": links,
        "fields": fields,
    }


def attachment_kind(url: str, label: str = "") -> str | None:
    path = urllib.parse.urlparse(url).path.lower()
    ext = Path(path).suffix
    if ext in ATTACHMENT_EXTENSIONS:
        if ext == ".pdf":
            return "PDF_DOCUMENT"
        if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tif", ".tiff"}:
            return "IMAGE"
        if ext in {".mp3", ".wav"}:
            return "AUDIO"
        if ext in {".mp4", ".mov", ".avi"}:
            return "VIDEO"
        if ext in {".zip"}:
            return "ARCHIVE"
        return "DOCUMENT_OR_DATA_FILE"
    haystack = (url + " " + label).lower()
    if "/sites/default/files/" in haystack or any(word in haystack for word in ("télécharger", "telecharger", "document", "annexe", "photo", "croquis")):
        return "LINKED_CASE_ASSET"
    return None


def extract_pdf_text(data: bytes) -> tuple[str, str | None]:
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data), strict=False)
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:  # noqa: BLE001
                pages.append(f"[PAGE_TEXT_ERROR: {type(exc).__name__}: {exc}]")
        return "\n\n".join(pages), None
    except Exception as exc:  # noqa: BLE001
        return "", f"{type(exc).__name__}: {exc}"


def discover_urls(root: Path) -> list[dict[str, str]]:
    found: dict[str, dict[str, str]] = {}
    url_rx = re.compile(r"https?://[^\s<>\"']+")
    relative_rx = re.compile(r"(?:href|url)[\"'=:\s]+(/fr/(?:cas|case)/[^\s<>\"']+)", flags=re.I)

    def add(url: str, source: str, source_id: str = "") -> None:
        url = normalize_url(url)
        if not url or not re.search(r"/(?:fr|en)/(?:cas|case)/", url, flags=re.I):
            return
        found.setdefault(url, {"case_url": url, "source_locator": source, "source_case_id": source_id})

    for path in root.rglob("*.csv"):
        try:
            with path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
                reader = csv.DictReader(handle)
                for row_number, row in enumerate(reader, start=2):
                    source_id = clean(next((v for k, v in row.items() if k and "id" in k.lower() and v), ""))
                    for value in row.values():
                        for url in url_rx.findall(clean(value)):
                            add(url, f"{path.name}:row:{row_number}", source_id)
        except Exception:
            continue
    for path in root.rglob("*.jsonl"):
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    obj = json.loads(line)
                    source_id = clean(obj.get("source_case_id") or obj.get("case_id") or obj.get("source_native_id")) if isinstance(obj, dict) else ""
                    for url in url_rx.findall(line):
                        add(url.rstrip(",}"), f"{path.name}:line:{line_number}", source_id)
        except Exception:
            continue
    for path in root.rglob("*.json"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            for url in url_rx.findall(text):
                add(url.rstrip(",}"), path.name)
            for rel in relative_rx.findall(text):
                add(rel, path.name)
        except Exception:
            continue
    for path in root.rglob("*.sqlite"):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            con.row_factory = sqlite3.Row
            tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            for table in tables:
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
                likely = [c for c in cols if any(t in c.lower() for t in ("url", "href", "link", "case_path", "case_url"))]
                if not likely:
                    continue
                selected = ",".join('"' + c.replace('"', '""') + '"' for c in likely)
                try:
                    for index, row in enumerate(con.execute(f'SELECT {selected} FROM "{table}"'), start=1):
                        for value in row:
                            for url in url_rx.findall(clean(value)):
                                add(url, f"{path.name}:{table}:{index}")
                except Exception:
                    continue
            con.close()
        except Exception:
            continue
    return sorted(found.values(), key=lambda row: row["case_url"])


def command_plan(args: argparse.Namespace) -> None:
    root = Path(args.live_index_dir)
    urls = discover_urls(root)
    if len(urls) != args.expected_cases:
        raise SystemExit(f"Expected {args.expected_cases} unique official case-detail URLs, found {len(urls)}")
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "case_urls.json").write_text(json.dumps(urls, indent=2, ensure_ascii=False), encoding="utf-8")
    include = []
    for start in range(0, len(urls), args.batch_size):
        batch_index = start // args.batch_size
        include.append({
            "batch_index": batch_index,
            "start": start,
            "end": min(start + args.batch_size, len(urls)),
            "expected_cases": min(args.batch_size, len(urls) - start),
        })
    matrix = {"include": include}
    (output / "matrix.json").write_text(json.dumps(matrix, separators=(",", ":")), encoding="utf-8")
    (output / "PLAN_SUMMARY.json").write_text(json.dumps({
        "status": "PASS", "case_urls": len(urls), "batch_size": args.batch_size,
        "batch_count": len(include), "source_policy": "Official GEIPAN case pages control",
    }, indent=2), encoding="utf-8")
    print(json.dumps(matrix, separators=(",", ":")))


def command_crawl(args: argparse.Namespace) -> None:
    urls = json.loads(Path(args.case_urls).read_text(encoding="utf-8"))
    subset = urls[args.start:args.end]
    output = Path(args.output_dir)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    blobs = output / "blobs"
    blobs.mkdir()
    db = output / f"GEIPAN_CASE_PACKET_BATCH_{args.batch_index:04d}.sqlite"
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""
    CREATE TABLE case_page(
      case_url TEXT PRIMARY KEY, source_case_id TEXT, source_locator TEXT,
      final_url TEXT, retrieval_status TEXT NOT NULL, retrieved_at TEXT,
      http_headers_json TEXT, html_bytes INTEGER, html_sha256 TEXT,
      html_gzip BLOB, page_title TEXT, meta_description TEXT,
      visible_text TEXT, visible_text_sha256 TEXT, error TEXT
    );
    CREATE TABLE case_page_field(
      field_id INTEGER PRIMARY KEY, case_url TEXT NOT NULL REFERENCES case_page(case_url),
      source_label TEXT NOT NULL, source_value TEXT NOT NULL
    );
    CREATE TABLE attachment(
      attachment_url TEXT PRIMARY KEY, case_url TEXT NOT NULL REFERENCES case_page(case_url),
      link_label TEXT, inferred_kind TEXT NOT NULL, retrieval_status TEXT NOT NULL,
      final_url TEXT, http_headers_json TEXT, byte_count INTEGER, sha256 TEXT,
      local_blob_path TEXT, mime_type TEXT, text_layer_status TEXT,
      extracted_text TEXT, extracted_text_sha256 TEXT, error TEXT
    );
    CREATE TABLE crawl_issue(
      issue_id INTEGER PRIMARY KEY, case_url TEXT, attachment_url TEXT,
      issue_code TEXT NOT NULL, severity TEXT NOT NULL, detail TEXT NOT NULL
    );
    CREATE INDEX idx_attachment_case ON attachment(case_url);
    CREATE INDEX idx_attachment_status ON attachment(retrieval_status);
    """)
    status_counts = Counter()
    attachment_counts = Counter()
    total_attachment_bytes = 0
    for index, item in enumerate(subset, start=1):
        case_url = item["case_url"]
        try:
            data, final_url, headers = request_bytes(case_url)
            parsed = parse_page(data, final_url)
            con.execute(
                "INSERT INTO case_page VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    case_url, item.get("source_case_id"), item.get("source_locator"),
                    final_url, "DOWNLOADED", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    json.dumps(headers, ensure_ascii=False, sort_keys=True), len(data), sha256_bytes(data),
                    gzip.compress(data, compresslevel=9), parsed["title"], parsed["meta_description"],
                    parsed["text"], sha256_bytes(parsed["text"].encode("utf-8")), None,
                ),
            )
            status_counts["DOWNLOADED"] += 1
            con.executemany(
                "INSERT INTO case_page_field(case_url,source_label,source_value) VALUES(?,?,?)",
                [(case_url, row["label"], row["value"]) for row in parsed["fields"]],
            )
            attachment_links: dict[str, tuple[str, str]] = {}
            for link in parsed["links"]:
                kind = attachment_kind(link["url"], link["label"])
                if kind:
                    attachment_links.setdefault(link["url"], (link["label"], kind))
            for attachment_url, (label, kind) in sorted(attachment_links.items()):
                if total_attachment_bytes >= args.max_batch_attachment_bytes:
                    con.execute(
                        "INSERT OR IGNORE INTO attachment(attachment_url,case_url,link_label,inferred_kind,retrieval_status,error) VALUES(?,?,?,?,?,?)",
                        (attachment_url, case_url, label, kind, "DEFERRED_BATCH_BYTE_CAP", "Batch attachment-byte cap reached"),
                    )
                    attachment_counts["DEFERRED_BATCH_BYTE_CAP"] += 1
                    continue
                try:
                    asset, asset_final_url, asset_headers = request_bytes(attachment_url)
                    if len(asset) > args.max_attachment_bytes:
                        con.execute(
                            "INSERT OR IGNORE INTO attachment(attachment_url,case_url,link_label,inferred_kind,retrieval_status,final_url,http_headers_json,byte_count,error) VALUES(?,?,?,?,?,?,?,?,?)",
                            (attachment_url, case_url, label, kind, "DEFERRED_INDIVIDUAL_BYTE_CAP", asset_final_url, json.dumps(asset_headers, ensure_ascii=False), len(asset), "Individual attachment exceeds byte cap"),
                        )
                        attachment_counts["DEFERRED_INDIVIDUAL_BYTE_CAP"] += 1
                        continue
                    digest = sha256_bytes(asset)
                    ext = Path(urllib.parse.urlparse(asset_final_url).path).suffix.lower()
                    if ext not in ATTACHMENT_EXTENSIONS:
                        ext = mimetypes.guess_extension(asset_headers.get("Content-Type", "").split(";", 1)[0]) or ".bin"
                    local_name = f"{digest}{ext}"
                    local_path = blobs / local_name
                    if not local_path.exists():
                        local_path.write_bytes(asset)
                    mime = asset_headers.get("Content-Type") or mimetypes.guess_type(local_name)[0]
                    extracted_text = ""
                    text_status = "NOT_APPLICABLE"
                    text_error = None
                    if kind == "PDF_DOCUMENT" or ext == ".pdf":
                        extracted_text, text_error = extract_pdf_text(asset)
                        text_status = "TEXT_LAYER_EXTRACTED" if clean(extracted_text) else "NO_TEXT_LAYER_OR_PARSE_ERROR"
                    con.execute(
                        "INSERT OR IGNORE INTO attachment VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            attachment_url, case_url, label, kind, "DOWNLOADED", asset_final_url,
                            json.dumps(asset_headers, ensure_ascii=False, sort_keys=True), len(asset), digest,
                            f"blobs/{local_name}", mime, text_status, extracted_text or None,
                            sha256_bytes(extracted_text.encode("utf-8")) if extracted_text else None,
                            text_error,
                        ),
                    )
                    total_attachment_bytes += len(asset)
                    attachment_counts["DOWNLOADED"] += 1
                    time.sleep(args.asset_delay_seconds)
                except Exception as exc:  # noqa: BLE001
                    con.execute(
                        "INSERT OR IGNORE INTO attachment(attachment_url,case_url,link_label,inferred_kind,retrieval_status,error) VALUES(?,?,?,?,?,?)",
                        (attachment_url, case_url, label, kind, "DOWNLOAD_ERROR", f"{type(exc).__name__}: {exc}"),
                    )
                    con.execute(
                        "INSERT INTO crawl_issue(case_url,attachment_url,issue_code,severity,detail) VALUES(?,?,?,?,?)",
                        (case_url, attachment_url, "ATTACHMENT_DOWNLOAD_ERROR", "MEDIUM", f"{type(exc).__name__}: {exc}"),
                    )
                    attachment_counts["DOWNLOAD_ERROR"] += 1
        except Exception as exc:  # noqa: BLE001
            con.execute(
                "INSERT INTO case_page(case_url,source_case_id,source_locator,retrieval_status,error) VALUES(?,?,?,?,?)",
                (case_url, item.get("source_case_id"), item.get("source_locator"), "DOWNLOAD_ERROR", f"{type(exc).__name__}: {exc}"),
            )
            con.execute(
                "INSERT INTO crawl_issue(case_url,issue_code,severity,detail) VALUES(?,?,?,?)",
                (case_url, "CASE_PAGE_DOWNLOAD_ERROR", "HIGH", f"{type(exc).__name__}: {exc}"),
            )
            status_counts["DOWNLOAD_ERROR"] += 1
        con.commit()
        print(f"batch {args.batch_index}: {index}/{len(subset)} {case_url} {dict(status_counts)}", flush=True)
        time.sleep(args.page_delay_seconds)
    quick = con.execute("PRAGMA quick_check").fetchone()[0]
    fk = len(con.execute("PRAGMA foreign_key_check").fetchall())
    pages = con.execute("SELECT COUNT(*) FROM case_page").fetchone()[0]
    con.close()
    summary = {
        "batch_index": args.batch_index, "start": args.start, "end": args.end,
        "expected_cases": len(subset), "case_page_rows": pages,
        "case_status_counts": dict(status_counts), "attachment_status_counts": dict(attachment_counts),
        "downloaded_attachment_bytes": total_attachment_bytes,
        "sqlite_quick_check": quick, "foreign_key_violations": fk,
        "source_policy": "Official GEIPAN page or attachment controls; no OCR was used.",
    }
    (output / "BATCH_SUMMARY.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    checksums = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            checksums.append(f"{sha256_file(path)}  {path.relative_to(output).as_posix()}")
    (output / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    if quick != "ok" or fk or pages != len(subset):
        raise SystemExit(json.dumps(summary, indent=2))


def command_aggregate(args: argparse.Namespace) -> None:
    source = Path(args.input_dir)
    output = Path(args.output_dir)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    db = output / "GEIPAN_CASE_PACKET_AND_ATTACHMENT_SNAPSHOT_v0.1.0.sqlite"
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""
    CREATE TABLE case_page(
      case_url TEXT PRIMARY KEY, source_case_id TEXT, source_locator TEXT,
      final_url TEXT, retrieval_status TEXT NOT NULL, retrieved_at TEXT,
      http_headers_json TEXT, html_bytes INTEGER, html_sha256 TEXT,
      html_gzip BLOB, page_title TEXT, meta_description TEXT,
      visible_text TEXT, visible_text_sha256 TEXT, error TEXT
    );
    CREATE TABLE case_page_field(field_id INTEGER PRIMARY KEY,case_url TEXT NOT NULL REFERENCES case_page(case_url),source_label TEXT NOT NULL,source_value TEXT NOT NULL);
    CREATE TABLE attachment(
      attachment_url TEXT PRIMARY KEY, case_url TEXT NOT NULL REFERENCES case_page(case_url),
      link_label TEXT, inferred_kind TEXT NOT NULL, retrieval_status TEXT NOT NULL,
      final_url TEXT, http_headers_json TEXT, byte_count INTEGER, sha256 TEXT,
      local_blob_path TEXT, mime_type TEXT, text_layer_status TEXT,
      extracted_text TEXT, extracted_text_sha256 TEXT, error TEXT
    );
    CREATE TABLE crawl_issue(issue_id INTEGER PRIMARY KEY,case_url TEXT,attachment_url TEXT,issue_code TEXT NOT NULL,severity TEXT NOT NULL,detail TEXT NOT NULL);
    CREATE INDEX idx_attachment_case ON attachment(case_url);
    CREATE INDEX idx_attachment_status ON attachment(retrieval_status);
    """)
    batch_summaries = []
    blob_dir = output / "blobs"
    blob_dir.mkdir()
    for batch_db in sorted(source.rglob("GEIPAN_CASE_PACKET_BATCH_*.sqlite")):
        batch_summaries.extend(
            json.loads(path.read_text(encoding="utf-8"))
            for path in batch_db.parent.glob("BATCH_SUMMARY.json")
        )
        bcon = sqlite3.connect(f"file:{batch_db}?mode=ro", uri=True)
        bcon.row_factory = sqlite3.Row
        for row in bcon.execute("SELECT * FROM case_page"):
            con.execute("INSERT OR REPLACE INTO case_page VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row))
        for row in bcon.execute("SELECT case_url,source_label,source_value FROM case_page_field"):
            con.execute("INSERT INTO case_page_field(case_url,source_label,source_value) VALUES(?,?,?)", tuple(row))
        for row in bcon.execute("SELECT * FROM attachment"):
            data = dict(row)
            local = data.get("local_blob_path")
            if local:
                src_blob = batch_db.parent / local
                if src_blob.exists():
                    dest = blob_dir / src_blob.name
                    if not dest.exists():
                        shutil.copy2(src_blob, dest)
                    data["local_blob_path"] = f"blobs/{dest.name}"
            con.execute(
                "INSERT OR REPLACE INTO attachment VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(data[key] for key in (
                    "attachment_url", "case_url", "link_label", "inferred_kind", "retrieval_status",
                    "final_url", "http_headers_json", "byte_count", "sha256", "local_blob_path",
                    "mime_type", "text_layer_status", "extracted_text", "extracted_text_sha256", "error",
                )),
            )
        for row in bcon.execute("SELECT case_url,attachment_url,issue_code,severity,detail FROM crawl_issue"):
            con.execute("INSERT INTO crawl_issue(case_url,attachment_url,issue_code,severity,detail) VALUES(?,?,?,?,?)", tuple(row))
        bcon.close()
        con.commit()
    quick = con.execute("PRAGMA quick_check").fetchone()[0]
    fk = len(con.execute("PRAGMA foreign_key_check").fetchall())
    page_count = con.execute("SELECT COUNT(*) FROM case_page").fetchone()[0]
    case_status = dict(con.execute("SELECT retrieval_status,COUNT(*) FROM case_page GROUP BY retrieval_status"))
    attachment_status = dict(con.execute("SELECT retrieval_status,COUNT(*) FROM attachment GROUP BY retrieval_status"))
    attachment_count = con.execute("SELECT COUNT(*) FROM attachment").fetchone()[0]
    downloaded_bytes = con.execute("SELECT COALESCE(SUM(byte_count),0) FROM attachment WHERE retrieval_status='DOWNLOADED'").fetchone()[0]
    con.close()
    summary = {
        "overall_status": "PASS" if quick == "ok" and fk == 0 and page_count == args.expected_cases else "FAIL",
        "expected_cases": args.expected_cases, "case_page_rows": page_count,
        "case_page_status_counts": case_status, "attachment_rows": attachment_count,
        "attachment_status_counts": attachment_status, "downloaded_attachment_bytes": downloaded_bytes,
        "sqlite_quick_check": quick, "foreign_key_violations": fk,
        "batch_count": len(batch_summaries), "batch_summaries": batch_summaries,
        "source_policy": "Official GEIPAN pages and attached files control. Original French is preserved; no OCR was used.",
    }
    (output / "SUMMARY.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    with (output / "CASE_PAGE_INDEX.csv").open("w", encoding="utf-8", newline="") as handle:
        con2 = sqlite3.connect(db)
        cur = con2.execute("SELECT case_url,source_case_id,final_url,retrieval_status,html_bytes,html_sha256,page_title,error FROM case_page ORDER BY case_url")
        writer = csv.writer(handle); writer.writerow([d[0] for d in cur.description]); writer.writerows(cur); con2.close()
    checksums = []
    for path in sorted(output.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.txt":
            checksums.append(f"{sha256_file(path)}  {path.relative_to(output).as_posix()}")
    (output / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")
    if summary["overall_status"] != "PASS":
        raise SystemExit(json.dumps(summary, indent=2))


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser()
    commands = root.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--live-index-dir", required=True)
    plan.add_argument("--output-dir", required=True)
    plan.add_argument("--expected-cases", type=int, default=3381)
    plan.add_argument("--batch-size", type=int, default=100)
    plan.set_defaults(function=command_plan)
    crawl = commands.add_parser("crawl")
    crawl.add_argument("--case-urls", required=True)
    crawl.add_argument("--batch-index", type=int, required=True)
    crawl.add_argument("--start", type=int, required=True)
    crawl.add_argument("--end", type=int, required=True)
    crawl.add_argument("--output-dir", required=True)
    crawl.add_argument("--page-delay-seconds", type=float, default=8.0)
    crawl.add_argument("--asset-delay-seconds", type=float, default=2.0)
    crawl.add_argument("--max-attachment-bytes", type=int, default=150_000_000)
    crawl.add_argument("--max-batch-attachment-bytes", type=int, default=1_000_000_000)
    crawl.set_defaults(function=command_crawl)
    aggregate = commands.add_parser("aggregate")
    aggregate.add_argument("--input-dir", required=True)
    aggregate.add_argument("--output-dir", required=True)
    aggregate.add_argument("--expected-cases", type=int, default=3381)
    aggregate.set_defaults(function=command_aggregate)
    return root


def main() -> None:
    args = parser().parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
