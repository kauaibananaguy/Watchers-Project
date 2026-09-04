#!/usr/bin/env python3
"""Run the GEIPAN live-index crawler with conservative rate limiting.

GEIPAN returns HTTP 429 after sustained rapid requests. This wrapper keeps the
same source-preservation logic but applies long cooldowns, Retry-After support,
and official-host fallback so the crawl can complete without hammering the
public service.
"""
from __future__ import annotations

import argparse
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

import crawl_live_case_index as base


def host_candidates(url: str) -> list[str]:
    parsed = urllib.parse.urlsplit(url)
    hosts = [parsed.netloc]
    for host in ("www.geipan.fr", "geipan.fr", "www.cnes-geipan.fr"):
        if host not in hosts:
            hosts.append(host)
    return [urllib.parse.urlunsplit((parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)) for host in hosts]


def resilient_fetch(url: str, retries: int = 30) -> tuple[bytes, str, dict[str, str]]:
    errors: list[str] = []
    attempt = 0
    while attempt < retries:
        for candidate in host_candidates(url):
            try:
                request = urllib.request.Request(
                    candidate,
                    headers={
                        "User-Agent": "Mozilla/5.0 (compatible; Watchers-UFO-Atlas/1.0; public-source-preservation)",
                        "Accept": "text/html,application/xhtml+xml,text/csv,*/*",
                        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.5",
                        "Connection": "close",
                    },
                )
                with urllib.request.urlopen(request, timeout=300) as response:
                    data = response.read()
                    if not data:
                        raise RuntimeError("empty response")
                    return data, response.geturl(), dict(response.headers.items())
            except urllib.error.HTTPError as exc:
                errors.append(f"{candidate}: HTTP {exc.code}")
                if exc.code == 429:
                    retry_after = exc.headers.get("Retry-After") if exc.headers else None
                    if retry_after and retry_after.isdigit():
                        cooldown = max(90, int(retry_after))
                    else:
                        cooldown = min(600, 120 + attempt * 30)
                    print(
                        f"GEIPAN rate limit on {candidate}; cooling down {cooldown}s before retry {attempt + 1}/{retries}",
                        flush=True,
                    )
                    time.sleep(cooldown + random.uniform(0, 10))
                    attempt += 1
                    break
                if exc.code in {500, 502, 503, 504}:
                    continue
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{candidate}: {type(exc).__name__}: {exc}")
                continue
        else:
            attempt += 1
            pause = min(120, 10 * max(1, attempt))
            print(f"All official hosts failed; waiting {pause}s before retry {attempt}/{retries}", flush=True)
            time.sleep(pause)
            continue
        # A 429 branch breaks the host loop after its cooldown. Continue outer loop.
        continue
    raise RuntimeError(f"Unable to fetch {url} after {retries} retry cycles:\n" + "\n".join(errors[-40:]))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="live_index_output")
    parser.add_argument("--package", default="GEIPAN_LIVE_CASE_INDEX_v0.1.0.zip")
    parser.add_argument("--delay-seconds", type=float, default=5.0)
    args = parser.parse_args()
    base.fetch = resilient_fetch
    base.build(args)


if __name__ == "__main__":
    main()
