#!/usr/bin/env python3
"""Download GEIPAN's live case and testimony export endpoints for format inspection."""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

URLS = {
    "cases": "https://www.geipan.fr/fr/cnes/export/cas",
    "testimonies": "https://www.geipan.fr/fr/cnes/export/temoignages",
}


def fetch(url: str) -> tuple[bytes, str, dict[str, str]]:
    last: Exception | None = None
    for attempt in range(12):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; Watchers-UFO-Atlas/1.0; public-source-preservation)",
                    "Accept": "*/*",
                    "Referer": "https://www.geipan.fr/fr/recherche/cas",
                    "Connection": "close",
                },
            )
            with urllib.request.urlopen(request, timeout=300) as response:
                data = response.read()
                if not data:
                    raise RuntimeError("empty response")
                return data, response.geturl(), dict(response.headers.items())
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < 12:
                time.sleep(120 if isinstance(exc, urllib.error.HTTPError) and exc.code == 429 else min(60, 2**attempt))
    raise RuntimeError(f"Unable to fetch {url}: {last}")


def main() -> None:
    output = Path("export_probe_output")
    output.mkdir(exist_ok=True)
    report = {}
    for name, url in URLS.items():
        data, final_url, headers = fetch(url)
        disposition = headers.get("Content-Disposition", "")
        content_type = headers.get("Content-Type", "")
        suffix = ".bin"
        lowered = disposition.lower()
        for candidate in (".xlsx", ".xls", ".csv", ".zip", ".json"):
            if candidate in lowered or candidate in content_type.lower():
                suffix = candidate
                break
        path = output / f"GEIPAN_live_export_{name}{suffix}"
        path.write_bytes(data)
        report[name] = {
            "requested_url": url,
            "final_url": final_url,
            "headers": headers,
            "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "saved_as": path.name,
            "first_64_bytes_hex": data[:64].hex(),
        }
        print(name, len(data), content_type, disposition, path.name, flush=True)
    (output / "EXPORT_ENDPOINT_PROBE.json").write_text(json.dumps(report, indent=2, ensure_ascii=False)+"\n", encoding="utf-8")


if __name__ == "__main__":
    main()
