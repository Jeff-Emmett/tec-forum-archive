"""Polite HTTP for archive.org and Common Crawl: retries, backoff, a floor on spacing."""
import json
import os
import time
import urllib.error
import urllib.request

UA = "tec-forum-archive/1.0 (preservation of forum.tecommons.org; contact via gitea.jeffemmett.com/jeffemmett/tec-forum-archive)"

_last = [0.0]


def get(url, *, spacing=1.2, tries=8, headers=None, timeout=90):
    """GET url, returning (status, headers, body). Retries 429/5xx and network errors.

    404/410 come back as a status, not an exception: a missing capture is data.
    """
    delay = spacing
    for attempt in range(tries):
        wait = _last[0] + spacing - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.monotonic()
        req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            if e.code in (404, 410, 403):
                return e.code, dict(e.headers), e.read()
            if e.code not in (429, 500, 502, 503, 504, 520, 522, 524):
                raise
            retry_after = e.headers.get("Retry-After")
            delay = max(delay * 2, float(retry_after) if retry_after and retry_after.isdigit() else 0, 5)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
            delay = max(delay * 2, 5)
        time.sleep(min(delay, 300))
    raise RuntimeError(f"gave up after {tries} tries: {url}")


def read_manifest(raw):
    """Rows of raw/manifest.jsonl; a half-written last line (interrupted fetch) is skipped."""
    rows = []
    path = os.path.join(raw, "manifest.jsonl")
    if os.path.exists(path):
        for line in open(path):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows
