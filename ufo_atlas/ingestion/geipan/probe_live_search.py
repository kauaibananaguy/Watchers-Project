#!/usr/bin/env python3
"""Capture GEIPAN live-search HTML and expose links, forms, and pagination."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


URLS = {
    "search_default": "https://www.geipan.fr/fr/recherche/cas",
    "search_tab": "https://www.geipan.fr/fr/recherche/cas/tab",
    "search_page_0": "https://www.geipan.fr/fr/recherche/cas?page=0",
    "search_page_1": "https://www.geipan.fr/fr/recherche/cas?page=1",
    "search_tab_page_0": "https://www.geipan.fr/fr/recherche/cas/tab?page=0",
    "search_tab_page_1": "https://www.geipan.fr/fr/recherche/cas/tab?page=1",
}


class ProbeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[dict[str, Any]] = []
        self.forms: list[dict[str, Any]] = []
        self.scripts: list[str] = []
        self._anchor: dict[str, Any] | None = None
        self._form: dict[str, Any] | None = None

    @staticmethod
    def attrs_dict(attrs: list[tuple[str, str | None]]) -> dict[str, str | None]:
        return dict(attrs)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = self.attrs_dict(attrs)
        if tag == "a" and values.get("href"):
            self._anchor = {"href": values.get("href"), "class": values.get("class"), "text": ""}
        elif tag == "form":
            self._form = {
                "action": values.get("action"),
                "method": values.get("method"),
                "id": values.get("id"),
                "class": values.get("class"),
                "controls": [],
            }
        elif tag in {"input", "select", "button"} and self._form is not None:
            self._form["controls"].append(
                {
                    "tag": tag,
                    "name": values.get("name"),
                    "id": values.get("id"),
                    "value": values.get("value"),
                    "type": values.get("type"),
                }
            )
        elif tag == "script" and values.get("src"):
            self.scripts.append(str(values["src"]))

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            self._anchor["text"] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            self._anchor["text"] = " ".join(self._anchor["text"].split())
            self.links.append(self._anchor)
            self._anchor = None
        elif tag == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None


def fetch(url: str) -> tuple[bytes, str, dict[str, str]]:
    last: Exception | None = None
    for attempt in range(6):
        try:
            request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; Watchers-UFO-Atlas/1.0; GEIPAN-index-preservation)",
                    "Accept": "text/html,application/xhtml+xml",
                },
            )
            with urllib.request.urlopen(request, timeout=180) as response:
                data = response.read()
                return data, response.geturl(), dict(response.headers.items())
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < 6:
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    wait = 15
                else:
                    wait = min(20, 2**attempt)
                time.sleep(wait)
    raise RuntimeError(f"Unable to fetch {url}: {last}")


def main() -> None:
    output = Path("probe_output")
    output.mkdir(exist_ok=True)
    report: dict[str, Any] = {"pages": {}}
    for name, url in URLS.items():
        data, final_url, headers = fetch(url)
        html_path = output / f"{name}.html"
        html_path.write_bytes(data)
        text = data.decode("utf-8", errors="replace")
        parser = ProbeParser()
        parser.feed(text)
        relevant_links = [
            link for link in parser.links
            if any(
                marker in str(link.get("href", "")).lower()
                for marker in ("/cas/", "/recherche/cas", "page=", "node/")
            )
        ]
        report["pages"][name] = {
            "requested_url": url,
            "final_url": final_url,
            "bytes": len(data),
            "headers": headers,
            "all_link_count": len(parser.links),
            "relevant_links": relevant_links,
            "forms": parser.forms,
            "scripts": parser.scripts,
            "drupal_settings_present": "drupalSettings" in text,
            "views_ajax_present": "views/ajax" in text,
            "pager_tokens": sorted({
                str(link["href"]) for link in parser.links
                if "page=" in str(link.get("href", ""))
            }),
        }
        print(name, len(data), len(relevant_links), final_url, flush=True)
    (output / "PROBE_REPORT.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
