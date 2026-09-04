#!/usr/bin/env python3
"""Capture representative GEIPAN case-detail pages and exposed linked files."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

BASE = "https://www.geipan.fr"
SAMPLES = {
    "recent_case": f"{BASE}/fr/cas/2026-04-51745",
    "early_case": f"{BASE}/fr/cas/1951-06-00003",
}


class Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, Any]] = []
        self.images: list[dict[str, Any]] = []
        self.meta: list[dict[str, Any]] = []
        self.headings: list[dict[str, Any]] = []
        self._anchor: dict[str, Any] | None = None
        self._heading: dict[str, Any] | None = None

    @staticmethod
    def values(attrs: list[tuple[str, str | None]]) -> dict[str, str | None]:
        return dict(attrs)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = self.values(attrs)
        if tag == "a" and values.get("href"):
            self._anchor = {"href": values.get("href"), "class": values.get("class"), "text": ""}
        elif tag == "img" and values.get("src"):
            self.images.append({"src": values.get("src"), "alt": values.get("alt"), "class": values.get("class")})
        elif tag == "meta":
            if values.get("name") or values.get("property"):
                self.meta.append(values)
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading = {"tag": tag, "class": values.get("class"), "text": ""}

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor["text"] += data
        if self._heading is not None:
            self._heading["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            self._anchor["text"] = " ".join(self._anchor["text"].split())
            self.links.append(self._anchor)
            self._anchor = None
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self._heading is not None:
            self._heading["text"] = " ".join(self._heading["text"].split())
            self.headings.append(self._heading)
            self._heading = None


def fetch(url: str) -> tuple[bytes, str, dict[str, str]]:
    last: Exception | None = None
    for attempt in range(6):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; Watchers-UFO-Atlas/1.0; GEIPAN-source-preservation)",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            with urllib.request.urlopen(request, timeout=180) as response:
                return response.read(), response.geturl(), dict(response.headers.items())
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < 6:
                time.sleep(12 if isinstance(exc, urllib.error.HTTPError) and exc.code == 429 else min(20, 2**attempt))
    raise RuntimeError(f"Unable to fetch {url}: {last}")


def main() -> None:
    output = Path("case_probe_output")
    output.mkdir(exist_ok=True)
    report: dict[str, Any] = {"samples": {}}
    for name, url in SAMPLES.items():
        try:
            data, final_url, headers = fetch(url)
            status = "FETCHED"
            error = None
        except Exception as exc:  # noqa: BLE001
            data = b""
            final_url = url
            headers = {}
            status = "ERROR"
            error = f"{type(exc).__name__}: {exc}"
        if data:
            (output / f"{name}.html").write_bytes(data)
        parser = Parser()
        parser.feed(data.decode("utf-8", errors="replace"))
        relevant = [
            link for link in parser.links
            if any(marker in str(link.get("href", "")).lower() for marker in (
                "/temoignage/", "/sites/default/files/", ".pdf", ".jpg", ".jpeg", ".png", ".mp4", ".zip", "/document"
            ))
        ]
        report["samples"][name] = {
            "requested_url": url,
            "final_url": final_url,
            "status": status,
            "error": error,
            "bytes": len(data),
            "headers": headers,
            "headings": parser.headings,
            "relevant_links": relevant,
            "all_links": parser.links,
            "images": parser.images,
            "meta": parser.meta,
        }
        print(name, status, len(data), len(relevant), flush=True)
    (output / "CASE_DETAIL_PROBE.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
