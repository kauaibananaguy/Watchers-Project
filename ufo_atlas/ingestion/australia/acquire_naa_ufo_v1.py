#!/usr/bin/env python3
"""Acquire Australian government UFO/UAP source files for the standalone UFO Atlas.

Primary authority: National Archives of Australia (NAA) / RecordSearch.
Discovery aid: Keith Basterfield's Australian Government UAP Files Listing
(Project 1947, 23 May 2016). The discovery aid never overrides NAA metadata.

The crawler preserves source identities and does not perform master-Atlas matching.
It produces a source-neutral SQLite package suitable for later UFO-spec extraction.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

INDEX_URL = "https://www.project1947.com/kbcat/kbuap2016.pdf"
NAA_ITEM = "https://recordsearch.naa.gov.au/SearchNRetrieve/Interface/DetailsReports/ItemDetail.aspx?Barcode={barcode}&isAv={available}"
NAA_VIEW = "https://recordsearch.naa.gov.au/SearchNRetrieve/Interface/ViewImage.aspx?B={barcode}"
NAA_IMAGE = "https://recordsearch.naa.gov.au/SearchNRetrieve/NAAMedia/ShowImage.aspx?B={barcode}&T=P&S={page}"
USER_AGENT = "Watchers-UFO-Atlas/1.0 (public archival research; low-rate source preservation)"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\x02", " ")).strip()


def download(session: requests.Session, url: str, path: Path, timeout: int = 180) -> tuple[int, str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    r = session.get(url, timeout=timeout, allow_redirects=True)
    ctype = r.headers.get("content-type", "")
    if r.ok and r.content:
        path.write_bytes(r.content)
    return r.status_code, r.url, ctype


def split_index_entries(text: str) -> list[tuple[int, str]]:
    # Section A is the publicly archived/digitised holdings list, records 1-123.
    text = text.replace("\x02", " ")
    start = text.find("SECTION A:")
    end = text.find("SECTION B:")
    if start >= 0:
        text = text[start:]
    if end >= 0:
        text = text[:end]
    matches = list(re.finditer(r"(?m)^\s*(\d{1,3})\s*\.\s*", text))
    if not matches:
        # Some PDF extractors omit the dot after a record number.
        matches = list(re.finditer(r"(?m)^\s*(\d{1,3})\s+(?=[A-Z\[])" , text))
    entries: list[tuple[int, str]] = []
    for i, m in enumerate(matches):
        n = int(m.group(1))
        if not 1 <= n <= 123:
            continue
        stop = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = clean(text[m.start():stop])
        entries.append((n, chunk))
    # PDF page headers can create duplicate apparent entry numbers; keep the longest.
    best: dict[int, str] = {}
    for n, chunk in entries:
        if len(chunk) > len(best.get(n, "")):
            best[n] = chunk
    return sorted(best.items())


def parse_index_entry(n: int, chunk: str) -> dict[str, Any]:
    series_match = re.search(r"\b([A-Z]{1,3}\d+(?:/\d+)?)\b", chunk)
    series = series_match.group(1) if series_match else None
    barcode = None
    b = re.search(r"\b(?:Canberra|Melbourne|Sydney|Adelaide|Darwin|Brisbane|Perth|AWM)\s+(\d{5,8})\b", chunk, re.I)
    if b:
        barcode = b.group(1)
    pages = None
    p = re.search(r"\b(\d{1,4})\s*(?:pp|PP)\b", chunk)
    if p:
        pages = int(p.group(1))
    access = None
    if re.search(r"Open\s+with\s+exception|\bOWE\b", chunk, re.I):
        access = "OPEN_WITH_EXCEPTION"
    elif re.search(r"\bOpen\b", chunk):
        access = "OPEN"
    digital = bool(re.search(r"NAA\s+digital\s+file", chunk, re.I))
    # Preserve the entire discovery entry; official NAA data later supersedes parsed fields.
    return {
        "discovery_entry_number": n,
        "series_hint": series,
        "barcode": barcode,
        "page_count_hint": pages,
        "access_hint": access,
        "naa_digital_hint": digital,
        "discovery_entry_text": chunk,
    }


def build_inventory(args: argparse.Namespace) -> None:
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    pdf = out / "AUSTRALIAN_GOVERNMENT_UAP_FILES_LISTING_2016.pdf"
    status, final_url, ctype = download(session, args.index_url, pdf)
    if status != 200 or not pdf.exists():
        raise SystemExit(f"Discovery index download failed: HTTP {status} {final_url} {ctype}")
    reader = PdfReader(str(pdf))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    (out / "DISCOVERY_INDEX_TEXT.txt").write_text(text, encoding="utf-8")
    entries = [parse_index_entry(n, chunk) for n, chunk in split_index_entries(text)]
    if len(entries) < 100:
        raise SystemExit(f"Index parse returned only {len(entries)} Section-A entries; refusing incomplete inventory")
    (out / "inventory.json").write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    with (out / "inventory.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(entries[0]))
        w.writeheader(); w.writerows(entries)
    summary = {
        "status": "PASS",
        "discovery_index_url": args.index_url,
        "discovery_index_sha256": sha256_file(pdf),
        "section_a_entries": len(entries),
        "entries_with_barcode": sum(bool(e["barcode"]) for e in entries),
        "entries_marked_naa_digital": sum(bool(e["naa_digital_hint"]) for e in entries),
        "policy": "Discovery aid identifies candidates only; NAA/RecordSearch remains source authority.",
    }
    (out / "SUMMARY.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


def extract_item_text(html: bytes) -> str:
    try:
        soup = BeautifulSoup(html, "html.parser")
        return clean(soup.get_text("\n"))
    except Exception:
        return ""


def acquire_item(args: argparse.Namespace) -> None:
    inventory = json.loads(Path(args.inventory).read_text(encoding="utf-8"))
    row = inventory[args.index]
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    barcode = row.get("barcode")
    result: dict[str, Any] = {**row, "inventory_index": args.index}
    if not barcode:
        result.update({"official_probe_status": "NO_BARCODE_IN_DISCOVERY_INDEX"})
        (out / "RESULT.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/152 Safari/537.36",
        "Accept-Language": "en-AU,en;q=0.9",
    })
    probes = []
    best_text = ""
    for available in ("Y", "N"):
        url = NAA_ITEM.format(barcode=barcode, available=available)
        try:
            r = session.get(url, timeout=120, allow_redirects=True)
            body = r.content
            text = extract_item_text(body)
            probes.append({"kind": f"ITEM_{available}", "requested_url": url, "status": r.status_code, "final_url": r.url, "content_type": r.headers.get("content-type"), "bytes": len(body), "text_sample": text[:2000]})
            if r.ok and text and "session expired" not in text.lower() and len(text) > len(best_text):
                best_text = text
                (out / f"NAA_ITEM_{available}.html").write_bytes(body)
        except Exception as e:
            probes.append({"kind": f"ITEM_{available}", "requested_url": url, "error": f"{type(e).__name__}: {e}"})
        time.sleep(args.delay)

    page_count = row.get("page_count_hint")
    view_url = NAA_VIEW.format(barcode=barcode)
    try:
        r = session.get(view_url, timeout=120, allow_redirects=True)
        view_text = r.text if "text" in r.headers.get("content-type", "") else ""
        probes.append({"kind": "VIEW", "requested_url": view_url, "status": r.status_code, "final_url": r.url, "content_type": r.headers.get("content-type"), "bytes": len(r.content)})
        if r.ok and view_text:
            (out / "NAA_VIEW.html").write_bytes(r.content)
            counts = [int(v) for v in re.findall(r"[?&]N=(\d+)", view_text)]
            if counts:
                page_count = max(counts)
    except Exception as e:
        probes.append({"kind": "VIEW", "requested_url": view_url, "error": f"{type(e).__name__}: {e}"})

    # Probe the first digital page. We deliberately do not bulk-republish scans here.
    image_url = NAA_IMAGE.format(barcode=barcode, page=1)
    try:
        r = session.get(image_url, timeout=180, allow_redirects=True)
        ctype = r.headers.get("content-type", "")
        probes.append({"kind": "PAGE_1", "requested_url": image_url, "status": r.status_code, "final_url": r.url, "content_type": ctype, "bytes": len(r.content), "sha256": hashlib.sha256(r.content).hexdigest() if r.ok and r.content else None})
        if r.ok and r.content and not ctype.lower().startswith("text/html"):
            suffix = ".jpg" if "jpeg" in ctype.lower() else ".bin"
            (out / f"NAA_PAGE_0001{suffix}").write_bytes(r.content)
    except Exception as e:
        probes.append({"kind": "PAGE_1", "requested_url": image_url, "error": f"{type(e).__name__}: {e}"})

    result.update({
        "official_probe_status": "PROBED",
        "official_item_text": best_text,
        "resolved_page_count": page_count,
        "official_recordsearch_item_url": NAA_ITEM.format(barcode=barcode, available="Y"),
        "official_recordsearch_view_url": view_url,
        "probes": probes,
    })
    (out / "RESULT.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    checks = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS.txt":
            checks.append(f"{sha256_file(p)}  {p.relative_to(out).as_posix()}")
    (out / "SHA256SUMS.txt").write_text("\n".join(checks) + "\n", encoding="utf-8")


def aggregate(args: argparse.Namespace) -> None:
    src = Path(args.input_dir)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for path in sorted(src.rglob("RESULT.json")):
        rows.append(json.loads(path.read_text(encoding="utf-8")))
    if not rows:
        raise SystemExit("No acquisition results found")
    db = out / "AUSTRALIA_NAA_UFO_SOURCE_SNAPSHOT_v0.1.0.sqlite"
    con = sqlite3.connect(db)
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript("""
    CREATE TABLE archive_item(
      item_id INTEGER PRIMARY KEY,
      discovery_entry_number INTEGER,
      series_hint TEXT,
      barcode TEXT,
      page_count_hint INTEGER,
      access_hint TEXT,
      naa_digital_hint INTEGER,
      discovery_entry_text TEXT NOT NULL,
      official_probe_status TEXT NOT NULL,
      official_item_text TEXT,
      resolved_page_count INTEGER,
      official_recordsearch_item_url TEXT,
      official_recordsearch_view_url TEXT,
      raw_json TEXT NOT NULL
    );
    CREATE UNIQUE INDEX idx_archive_barcode ON archive_item(barcode) WHERE barcode IS NOT NULL;
    CREATE TABLE access_probe(
      probe_id INTEGER PRIMARY KEY,
      item_id INTEGER NOT NULL REFERENCES archive_item(item_id),
      kind TEXT NOT NULL,
      requested_url TEXT,
      status INTEGER,
      final_url TEXT,
      content_type TEXT,
      byte_count INTEGER,
      sha256 TEXT,
      error TEXT,
      text_sample TEXT
    );
    """)
    for i, row in enumerate(sorted(rows, key=lambda r: int(r["discovery_entry_number"])), start=1):
        con.execute("INSERT INTO archive_item VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            i, row.get("discovery_entry_number"), row.get("series_hint"), row.get("barcode"), row.get("page_count_hint"), row.get("access_hint"), int(bool(row.get("naa_digital_hint"))), row.get("discovery_entry_text") or "", row.get("official_probe_status") or "UNKNOWN", row.get("official_item_text"), row.get("resolved_page_count"), row.get("official_recordsearch_item_url"), row.get("official_recordsearch_view_url"), json.dumps(row, ensure_ascii=False, sort_keys=True)
        ))
        for probe in row.get("probes", []):
            con.execute("INSERT INTO access_probe(item_id,kind,requested_url,status,final_url,content_type,byte_count,sha256,error,text_sample) VALUES(?,?,?,?,?,?,?,?,?,?)", (
                i, probe.get("kind"), probe.get("requested_url"), probe.get("status"), probe.get("final_url"), probe.get("content_type"), probe.get("bytes"), probe.get("sha256"), probe.get("error"), probe.get("text_sample")
            ))
    con.commit()
    quick = con.execute("PRAGMA quick_check").fetchone()[0]
    fk = con.execute("PRAGMA foreign_key_check").fetchall()
    counts = {
        "archive_items": con.execute("SELECT COUNT(*) FROM archive_item").fetchone()[0],
        "items_with_barcode": con.execute("SELECT COUNT(*) FROM archive_item WHERE barcode IS NOT NULL").fetchone()[0],
        "official_item_text_recovered": con.execute("SELECT COUNT(*) FROM archive_item WHERE official_item_text IS NOT NULL AND length(official_item_text)>0").fetchone()[0],
        "first_page_binary_reachable": con.execute("SELECT COUNT(DISTINCT item_id) FROM access_probe WHERE kind='PAGE_1' AND status=200 AND content_type NOT LIKE 'text/html%'").fetchone()[0],
        "access_probes": con.execute("SELECT COUNT(*) FROM access_probe").fetchone()[0],
    }
    con.close()
    summary = {"status": "PASS" if quick == "ok" and not fk else "FAIL", **counts, "sqlite_quick_check": quick, "foreign_key_violations": len(fk), "next_stage": "Acquire digitised contents from reachable NAA items and extract individual UFO cases under the standalone UFO specification."}
    (out / "SUMMARY.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    with (out / "ARCHIVE_ITEM_INDEX.csv").open("w", newline="", encoding="utf-8") as f:
        fields = ["discovery_entry_number","series_hint","barcode","page_count_hint","access_hint","naa_digital_hint","official_probe_status","resolved_page_count","official_recordsearch_item_url","official_recordsearch_view_url"]
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        for row in sorted(rows, key=lambda r: int(r["discovery_entry_number"])):
            w.writerow({k: row.get(k) for k in fields})
    checks = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS.txt":
            checks.append(f"{sha256_file(p)}  {p.relative_to(out).as_posix()}")
    (out / "SHA256SUMS.txt").write_text("\n".join(checks) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    if summary["status"] != "PASS":
        raise SystemExit(json.dumps(summary, indent=2))


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    a = sub.add_parser("inventory")
    a.add_argument("--output-dir", required=True)
    a.add_argument("--index-url", default=INDEX_URL)
    a.set_defaults(func=build_inventory)
    b = sub.add_parser("acquire")
    b.add_argument("--inventory", required=True)
    b.add_argument("--index", type=int, required=True)
    b.add_argument("--output-dir", required=True)
    b.add_argument("--delay", type=float, default=1.0)
    b.set_defaults(func=acquire_item)
    c = sub.add_parser("aggregate")
    c.add_argument("--input-dir", required=True)
    c.add_argument("--output-dir", required=True)
    c.set_defaults(func=aggregate)
    args = p.parse_args(); args.func(args)

if __name__ == "__main__":
    main()
