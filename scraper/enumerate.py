"""Stage 1: list every capture of forum.tecommons.org held by the Wayback Machine and Common Crawl.

    python3 -I scraper/enumerate.py data/sources

Writes wayback-cdx.json and commoncrawl-cdx.jsonl. Only indexes are fetched here; no page bodies.
"""
import json
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from net import get  # noqa: E402

HOST = "forum.tecommons.org"
FIRST_CC_YEAR = 2020  # the forum opened in 2020


def wayback(out_dir):
    q = urllib.parse.urlencode({
        "url": HOST, "matchType": "prefix", "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,digest,length",
    })
    status, _, body = get(f"https://web.archive.org/cdx/search/cdx?{q}", timeout=300)
    assert status == 200, status
    rows = json.loads(body)
    head, rows = rows[0], rows[1:]
    recs = [dict(zip(head, r)) for r in rows]
    with open(os.path.join(out_dir, "wayback-cdx.json"), "w") as f:
        json.dump(recs, f, indent=0)
    print(f"wayback: {len(recs)} captures")


def commoncrawl(out_dir):
    status, _, body = get("https://index.commoncrawl.org/collinfo.json")
    crawls = [c["id"] for c in json.loads(body)
              if c["id"].startswith("CC-MAIN-") and int(c["id"].split("-")[2]) >= FIRST_CC_YEAR]
    n = 0
    with open(os.path.join(out_dir, "commoncrawl-cdx.jsonl"), "w") as f:
        for crawl in crawls:
            q = urllib.parse.urlencode({"url": f"{HOST}/*", "output": "json"})
            status, _, body = get(f"https://index.commoncrawl.org/{crawl}-index?{q}", spacing=2, timeout=180)
            if status == 404:  # "No Captures found"
                continue
            lines = [ln for ln in body.decode().splitlines() if ln.strip()]
            for ln in lines:
                rec = json.loads(ln)
                rec["crawl"] = crawl
                f.write(json.dumps(rec) + "\n")
            n += len(lines)
            print(f"  {crawl}: {len(lines)}", flush=True)
    print(f"commoncrawl: {n} captures across {len(crawls)} crawls")


if __name__ == "__main__":
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    wayback(out)
    commoncrawl(out)
