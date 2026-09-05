"""Conservative GEIPAN acquisition failure policy (source staging only).

An incomplete network or archive lookup never proves permanent source absence.
The existing terminal states describe a completed source-availability check at
retrieval time, not an assertion that a resource can never become available.
"""
from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath

INCOMPLETE = "ARCHIVAL_RECOVERY_INCOMPLETE"
INCOMPLETE_TOKENS = (
    "archival_recovery_incomplete", "archival_recovery_failed",
    "archive_lookup_incomplete", "invalid_recovery_payload",
    "temporary failure in name resolution", "name or service not known",
    "nodename nor servname", "source_host_unresolvable", "getaddrinfo failed",
    "timed out", "timeout", "connection reset", "connection refused",
    "connection aborted", "network is unreachable", "sslerror",
    "certificate verify failed", "wayback error or exclusion page",
    "empty response",
)
TERMINAL_MARKERS = {
    "SOURCE_NOT_AVAILABLE_404", "SOURCE_FORBIDDEN_403", "SOURCE_GONE_410",
    "SOURCE_MALFORMED_LINK",
}
HTTP_CODE = re.compile(r"\b(?:http error|status code|http status)\s*[:=]?\s*(\d{3})\b", re.I)


def incomplete_evidence(text: str) -> bool:
    lower = text.lower()
    return any(token in lower for token in INCOMPLETE_TOKENS) or any(
        int(code) not in {403, 404, 410} for code in HTTP_CODE.findall(text)
    )


def classify_recovery_errors(errors: Sequence[str]) -> str:
    """Only all-explicit HTTP absence/denial results can close a lookup pass."""
    if not errors or any(incomplete_evidence(item) for item in errors):
        return INCOMPLETE
    codes: list[int] = []
    for item in errors:
        observed = {int(code) for code in HTTP_CODE.findall(item)}
        if len(observed) != 1 or not observed.issubset({403, 404, 410}):
            return INCOMPLETE
        codes.append(observed.pop())
    if all(code == 410 for code in codes):
        return "SOURCE_GONE_410"
    if 403 in codes:
        return "SOURCE_FORBIDDEN_403"
    return "SOURCE_NOT_AVAILABLE_404"


def map_terminal_status(status: str, error: str | None) -> str:
    """Do not promote an unqualified first-pass error to a terminal state."""
    if status == "DOWNLOADED":
        return status
    text = error or ""
    if incomplete_evidence(text):
        return "DOWNLOAD_ERROR"
    markers = re.findall(r"\b(SOURCE_[A-Z_0-9]+):", text.upper())
    if markers and markers[0] in TERMINAL_MARKERS:
        return markers[0]
    return status if status not in TERMINAL_MARKERS else "DOWNLOAD_ERROR"


def parse_cdx_capture_data(data: bytes) -> list[str]:
    """Separate an actual zero-capture result from unavailable/invalid CDX."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("ARCHIVE_LOOKUP_INCOMPLETE: invalid CDX JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError("ARCHIVE_LOOKUP_INCOMPLETE: unexpected CDX response")
    if not payload:
        return []
    if not isinstance(payload[0], list) or payload[0][:2] != ["timestamp", "original"]:
        raise RuntimeError("ARCHIVE_LOOKUP_INCOMPLETE: invalid CDX header")
    captures = []
    for row in reversed(payload[1:]):
        if not isinstance(row, list) or len(row) < 3:
            raise RuntimeError("ARCHIVE_LOOKUP_INCOMPLETE: malformed CDX row")
        timestamp, original, status = map(str, row[:3])
        parsed = urllib.parse.urlparse(original)
        if not re.fullmatch(r"\d{14}", timestamp) or parsed.scheme not in {"http", "https"} or not parsed.netloc or status != "200":
            raise RuntimeError("ARCHIVE_LOOKUP_INCOMPLETE: invalid capture identity")
        captures.append(f"https://web.archive.org/web/{timestamp}id_/{original}")
    return list(dict.fromkeys(captures))


def validate_recovered_payload(data: bytes, source_url: str, headers: Mapping[str, str]) -> None:
    """Reject HTML error pages masquerading as recovered binary documents."""
    if not data:
        raise RuntimeError("INVALID_RECOVERY_PAYLOAD: empty response")
    ext = PurePosixPath(urllib.parse.urlparse(source_url).path).suffix.lower()
    sample = data.lstrip()[:512].lower()
    mime = next((v.lower() for k, v in headers.items() if k.lower() == "content-type"), "")
    if ext == ".pdf" and not data.lstrip().startswith(b"%PDF-"):
        raise RuntimeError("INVALID_RECOVERY_PAYLOAD: expected PDF signature")
    binary_exts = {".pdf", ".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".webp", ".avi", ".mov", ".mp4", ".mp3", ".wav", ".zip", ".kmz", ".doc", ".docx", ".xls", ".xlsx"}
    if ext in binary_exts and ("text/html" in mime or sample.startswith((b"<!doctype html", b"<html"))):
        raise RuntimeError("INVALID_RECOVERY_PAYLOAD: HTML returned for a binary source")
