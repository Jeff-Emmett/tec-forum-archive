"""Stage 2: download the captures stage 1 listed, into a content-addressed raw store.

    python3 -I scraper/fetch.py data/sources raw

    python3 -I scraper/fetch.py data/sources raw 0/3   # shard 0 of 3, run 3 in parallel

Resumable: raw/manifest.jsonl records every fetch; re-running skips what it already has.
Selection:
  * topic pages (/t/...): every distinct body (by Wayback digest) -- posts are unioned later,
    and different snapshots hold different replies and different edits
  * listings, sitemaps, category pages: newest capture of each URL
  * uploads (images, attachments): newest capture of each URL
  * Common Crawl: every topic-page WARC record
"""
import gzip
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from net import get, read_manifest  # noqa: E402

LISTING = re.compile(r"^/(c/|categories|latest|top|new|tag/|tags|sitemap|about|$)")


def path_of(url):
    p = re.sub(r"^https?://(www\.)?forum\.tecommons\.org(:\d+)?", "", url)
    return p or "/"


def norm(url):
    p = path_of(url)
    p = p.split("#")[0]
    return p.rstrip("/") or "/"


def plan(src):
    wb = json.load(open(os.path.join(src, "wayback-cdx.json")))
    jobs, seen_digest, newest = [], set(), {}
    for r in wb:
        if r["statuscode"] != "200":
            continue
        p = norm(r["original"])
        if p.startswith("/t/"):
            key = (p, r["digest"])
            if key not in seen_digest and r["mimetype"] in ("text/html", "application/rss+xml", "application/json"):
                seen_digest.add(key)
                jobs.append(("wayback", r["timestamp"], r["original"], "topic", None))
        elif p.startswith(("/uploads/", "/images/emoji/")):
            key = ("upload", p.split("?")[0])
            newest[key] = max(newest.get(key, ("", "")), (r["timestamp"], r["original"]))
        elif LISTING.match(p) and r["mimetype"] in ("text/html", "text/xml", "application/xml", "application/json", "application/rss+xml"):
            newest[("listing", p)] = max(newest.get(("listing", p), ("", "")), (r["timestamp"], r["original"]))
    for (kind, _), (ts, url) in newest.items():
        jobs.append(("wayback", ts, url, kind, None))
    cc_path = os.path.join(src, "commoncrawl-cdx.jsonl")
    if os.path.exists(cc_path):
        for line in open(cc_path):
            r = json.loads(line)
            if r.get("status") == "200" and path_of(r["url"]).startswith("/t/"):
                jobs.append(("commoncrawl", r["timestamp"], r["url"], "topic",
                             {"filename": r["filename"], "offset": int(r["offset"]), "length": int(r["length"])}))
    return jobs


def warc_body(blob):
    """One gzip member = one WARC response record; return (http_status, body)."""
    data = gzip.decompress(blob)
    _, _, rest = data.partition(b"\r\n\r\n")          # WARC headers
    http_head, _, body = rest.partition(b"\r\n\r\n")   # HTTP headers
    status = int(http_head.split(b" ", 2)[1])
    return status, body


def main(src, raw, shard=(0, 1)):
    os.makedirs(os.path.join(raw, "bodies"), exist_ok=True)
    manifest_path = os.path.join(raw, "manifest.jsonl")
    done = set()
    if os.path.exists(manifest_path):
        for m in read_manifest(raw):
            done.add((m["source"], m["timestamp"], m["url"]))
    jobs = [j for j in plan(src) if (j[0], j[1], j[2]) not in done]
    jobs = jobs[shard[0]::shard[1]]
    print(f"{len(jobs)} to fetch ({len(done)} already done)", flush=True)
    with open(manifest_path, "a") as man:
        for i, (source, ts, url, kind, cc) in enumerate(jobs, 1):
            if source == "wayback":
                status, headers, body = get(f"https://web.archive.org/web/{ts}id_/{url}", spacing=1.5)
                ctype = headers.get("Content-Type", "")
            else:
                rng = f"bytes={cc['offset']}-{cc['offset'] + cc['length'] - 1}"
                status, _, blob = get(f"https://data.commoncrawl.org/{cc['filename']}", spacing=0.5, headers={"Range": rng})
                ctype = "text/html"
                if status in (200, 206):
                    status, body = warc_body(blob)
                else:
                    body = b""
            sha = hashlib.sha256(body).hexdigest()
            fn = os.path.join(raw, "bodies", sha[:2], sha + ".gz")
            if body and not os.path.exists(fn):
                os.makedirs(os.path.dirname(fn), exist_ok=True)
                with gzip.open(fn, "wb") as f:
                    f.write(body)
            man.write(json.dumps({"source": source, "timestamp": ts, "url": url, "kind": kind,
                                  "status": status, "content_type": ctype, "sha256": sha if body else None,
                                  "bytes": len(body)}) + "\n")
            man.flush()
            if i % 50 == 0 or i == len(jobs):
                print(f"  {i}/{len(jobs)}", flush=True)


if __name__ == "__main__":
    # optional "i/n": take every n-th job from i, so n processes can share one manifest
    i, n = map(int, sys.argv[3].split("/")) if len(sys.argv) > 3 else (0, 1)
    main(sys.argv[1], sys.argv[2], (i, n))
