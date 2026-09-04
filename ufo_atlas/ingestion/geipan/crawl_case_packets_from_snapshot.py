#!/usr/bin/env python3
"""Plan GEIPAN case-packet acquisition directly from the validated source snapshot."""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

import crawl_case_packets as base


def discover_with_workbooks(root: Path) -> list[dict[str, str]]:
    found = {row["case_url"]: row for row in base.discover_urls(root)}

    def add(url: str, source: str, source_id: str = "") -> None:
        url = base.normalize_url(url)
        if not re.search(r"/(?:fr|en)/(?:cas|case)/", url, flags=re.I):
            return
        found.setdefault(url, {"case_url": url, "source_locator": source, "source_case_id": source_id})

    url_rx = re.compile(r"https?://[^\s<>\"']+")
    for path in root.rglob("*.xlsx"):
        try:
            workbook = load_workbook(path, read_only=False, data_only=False, keep_links=True)
        except Exception:
            continue
        for worksheet in workbook.worksheets:
            header_values: list[str] = []
            for row_index, row in enumerate(worksheet.iter_rows(), start=1):
                values = [base.clean(cell.value) for cell in row]
                if row_index == 1:
                    header_values = values
                source_id = ""
                for index, value in enumerate(values):
                    header = header_values[index] if index < len(header_values) else ""
                    if value and any(token in header.lower() for token in ("id", "numéro", "numero", "référence", "reference")):
                        source_id = value
                        break
                for cell in row:
                    if cell.hyperlink and cell.hyperlink.target:
                        add(cell.hyperlink.target, f"{path.name}:{worksheet.title}:row:{row_index}", source_id)
                    for url in url_rx.findall(base.clean(cell.value)):
                        add(url, f"{path.name}:{worksheet.title}:row:{row_index}", source_id)
    return sorted(found.values(), key=lambda row: row["case_url"])


def command_plan(args: argparse.Namespace) -> None:
    root = Path(args.live_index_dir)
    urls = discover_with_workbooks(root)
    if len(urls) != args.expected_cases:
        diagnostic = {
            "status": "FAIL",
            "expected_case_urls": args.expected_cases,
            "discovered_case_urls": len(urls),
            "first_urls": urls[:20],
            "source_files": [str(path.relative_to(root)) for path in sorted(root.rglob("*")) if path.is_file()][:500],
        }
        output = Path(args.output_dir)
        output.mkdir(parents=True, exist_ok=True)
        (output / "URL_DISCOVERY_DIAGNOSTIC.json").write_text(json.dumps(diagnostic, indent=2, ensure_ascii=False), encoding="utf-8")
        raise SystemExit(json.dumps(diagnostic, indent=2, ensure_ascii=False))
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "case_urls.json").write_text(json.dumps(urls, indent=2, ensure_ascii=False), encoding="utf-8")
    include = []
    for start in range(0, len(urls), args.batch_size):
        include.append({
            "batch_index": start // args.batch_size,
            "start": start,
            "end": min(start + args.batch_size, len(urls)),
            "expected_cases": min(args.batch_size, len(urls) - start),
        })
    matrix = {"include": include}
    (output / "matrix.json").write_text(json.dumps(matrix, separators=(",", ":")), encoding="utf-8")
    (output / "PLAN_SUMMARY.json").write_text(json.dumps({
        "status": "PASS", "case_urls": len(urls), "batch_size": args.batch_size,
        "batch_count": len(include), "source": "validated current-and-legacy GEIPAN source snapshot",
    }, indent=2), encoding="utf-8")
    print(json.dumps(matrix, separators=(",", ":")))


def main() -> None:
    parser = base.parser()
    args = parser.parse_args()
    if args.command == "plan":
        command_plan(args)
    else:
        args.function(args)


if __name__ == "__main__":
    main()
