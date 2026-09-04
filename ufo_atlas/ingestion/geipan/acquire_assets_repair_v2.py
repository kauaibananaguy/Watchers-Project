#!/usr/bin/env python3
"""Recover GEIPAN linked assets from live URL variants and public web archives.

This wrapper is used only for explicit remands created by the first acquisition
pass. It preserves the source URL as the asset identity while allowing the
binary to be recovered from a corrected live URL or an archived copy. Recovery
provenance is returned in synthetic HTTP-header fields and is therefore retained
by the existing linked-asset database builder.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import crawl_assets_v1 as base
import crawl_case_packets as shared

ORIGINAL_REQUEST_BYTES = shared.request_bytes
USER_AGENT = "Watchers-UFO-Atlas/1.0 (source-remand-recovery; low-rate archival lookup)"
OFFICIAL_HOSTS = {
    "geipan.fr",
    "www.geipan.fr",
    "cnes-geipan.fr",
    "www.cnes-geipan.fr",
}


def unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def malformed_source_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    leaf = Path(parsed.path).name
    return (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or leaf.lower()
        in {".pdf", ".mov", ".avi", ".jpg", ".jpeg", ".png", ".doc", ".docx"}
    )


def live_candidates(source_url: str) -> list[str]:
    """Generate conservative live variants without changing source identity."""
    parsed = urllib.parse.urlparse(source_url)
    candidates = [source_url]

    if parsed.scheme == "http":
        candidates.append(urllib.parse.urlunparse(parsed._replace(scheme="https")))

    host = parsed.netloc.lower()
    path = parsed.path
    query = parsed.query

    if host in OFFICIAL_HOSTS:
        for new_host in (
            "www.geipan.fr",
            "geipan.fr",
            "www.cnes-geipan.fr",
            "cnes-geipan.fr",
        ):
            candidates.append(
                urllib.parse.urlunparse(
                    ("https", new_host, path, parsed.params, query, parsed.fragment)
                )
            )
        migrated_paths = [path]
        for prefix in (
            "/fileadmin/documents/",
            "/fileadmin/geipan-doc/",
            "/fileadmin/user_upload/",
        ):
            if path.startswith(prefix):
                migrated_paths.append("/sites/default/files/" + path[len(prefix) :])
        for migrated in migrated_paths:
            for new_host in (
                "www.geipan.fr",
                "geipan.fr",
                "www.cnes-geipan.fr",
            ):
                candidates.append(
                    urllib.parse.urlunparse(
                        (
                            "https",
                            new_host,
                            migrated,
                            parsed.params,
                            query,
                            parsed.fragment,
                        )
                    )
                )

    try:
        decoded = urllib.parse.unquote(path)
        encoded = urllib.parse.quote(decoded, safe="/:@!$&'()*+,;=-._~")
        if encoded != path:
            candidates.append(
                urllib.parse.urlunparse(
                    (
                        parsed.scheme or "https",
                        parsed.netloc,
                        encoded,
                        parsed.params,
                        query,
                        parsed.fragment,
                    )
                )
            )
    except Exception:
        pass

    return unique(candidates)


def simple_request(
    url: str, timeout: int = 180
) -> tuple[bytes, str, dict[str, str]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/pdf,image/*,video/*,audio/*,application/octet-stream,text/html,*/*;q=0.8",
            "Accept-Language": "fr,en;q=0.8",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
        if not data:
            raise RuntimeError("empty response")
        headers = {str(k): str(v) for k, v in response.headers.items()}
        return data, response.geturl(), headers


def cdx_capture_urls(original_url: str) -> list[str]:
    """Return newest public Internet Archive captures for an exact URL."""
    params = urllib.parse.urlencode(
        {
            "url": original_url,
            "output": "json",
            "filter": "statuscode:200",
            "fl": "timestamp,original,statuscode,mimetype,digest,length",
            "limit": "-10",
        }
    )
    cdx_url = "https://web.archive.org/cdx/search/cdx?" + params
    try:
        data, _, _ = simple_request(cdx_url, timeout=120)
        payload = json.loads(data.decode("utf-8", errors="replace"))
    except Exception:
        return []
    if not isinstance(payload, list) or len(payload) < 2:
        return []
    captures: list[str] = []
    for row in reversed(payload[1:]):
        if not isinstance(row, list) or len(row) < 2:
            continue
        timestamp = str(row[0])
        archived_original = str(row[1])
        captures.append(
            f"https://web.archive.org/web/{timestamp}id_/{archived_original}"
        )
    return unique(captures)


def is_wayback_error_page(data: bytes, headers: dict[str, str]) -> bool:
    content_type = headers.get("Content-Type", "").lower()
    if "text/html" not in content_type:
        return False
    sample = data[:200000].lower()
    markers = (
        b"this url has been excluded from the wayback machine",
        b"the wayback machine has not archived that url",
        b"page cannot be crawled or displayed due to robots.txt",
        b"internal server error (web.archive.org)",
    )
    return any(marker in sample for marker in markers)


def classify_errors(errors: list[str]) -> str:
    text = "\n".join(errors).lower()
    if "http error 410" in text:
        return "SOURCE_GONE_410"
    if "http error 404" in text:
        return "SOURCE_NOT_AVAILABLE_404"
    if "http error 403" in text:
        return "SOURCE_FORBIDDEN_403"
    if any(
        token in text
        for token in (
            "name or service not known",
            "temporary failure in name resolution",
            "nodename nor servname",
        )
    ):
        return "SOURCE_HOST_UNRESOLVABLE"
    return "ARCHIVAL_RECOVERY_FAILED"


def resilient_request_bytes(
    source_url: str,
    retries: int = 9,
    timeout: int = 300,
) -> tuple[bytes, str, dict[str, str]]:
    if malformed_source_url(source_url):
        raise RuntimeError(f"SOURCE_MALFORMED_LINK: {source_url}")

    errors: list[str] = []
    candidates = live_candidates(source_url)

    for candidate in candidates:
        try:
            data, final_url, headers = ORIGINAL_REQUEST_BYTES(
                candidate,
                retries=max(1, min(int(retries), 3)),
                timeout=min(int(timeout), 300),
            )
            headers = dict(headers)
            headers.update(
                {
                    "X-Watchers-Acquisition-Method": "LIVE_URL_VARIANT",
                    "X-Watchers-Original-Source-URL": source_url,
                    "X-Watchers-Requested-Recovery-URL": candidate,
                }
            )
            return data, final_url, headers
        except Exception as exc:  # noqa: BLE001
            errors.append(f"LIVE {candidate}: {type(exc).__name__}: {exc}")

    archive_queries = (
        candidates
        if urllib.parse.urlparse(source_url).netloc.lower() in OFFICIAL_HOSTS
        else candidates[:2]
    )
    archive_seen: set[str] = set()
    for archive_source in archive_queries:
        for capture_url in cdx_capture_urls(archive_source):
            if capture_url in archive_seen:
                continue
            archive_seen.add(capture_url)
            try:
                data, final_url, headers = simple_request(
                    capture_url, timeout=min(int(timeout), 300)
                )
                if is_wayback_error_page(data, headers):
                    raise RuntimeError("Wayback error or exclusion page")
                headers = dict(headers)
                headers.update(
                    {
                        "X-Watchers-Acquisition-Method": "INTERNET_ARCHIVE_PUBLIC_CAPTURE",
                        "X-Watchers-Original-Source-URL": source_url,
                        "X-Watchers-Archived-Source-URL": archive_source,
                        "X-Watchers-Archive-Capture-URL": capture_url,
                    }
                )
                return data, final_url, headers
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"ARCHIVE {capture_url}: {type(exc).__name__}: {exc}"
                )
        time.sleep(0.5)

    marker = classify_errors(errors)
    compact = " | ".join(errors[-12:])
    raise RuntimeError(
        f"{marker}: no recoverable live variant or public archived capture for "
        f"{source_url}. {compact}"
    )


def main() -> None:
    shared.request_bytes = resilient_request_bytes
    args = base.parser().parse_args()
    args.function(args)


if __name__ == "__main__":
    main()
