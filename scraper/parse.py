"""Stage 3: turn raw captures into one merged record per topic.

    python3 -I scraper/parse.py raw data

Every capture is read as a bag of posts. Posts are unioned by post_number across captures
and sources; where several captures hold the same post, the newest capture wins (it carries
the latest edit). Each post keeps the capture it came from, so every line in the archive can
be traced back to a Wayback or Common Crawl record.

Two page shapes occur:
  * newer Discourse (2023+): a `data-preloaded` blob with the topic JSON and the site's
    category table -- preferred, it has display names, reply links and like counts
  * the crawler view (every capture, incl. inside <noscript>): schema.org-marked posts
Plus topic RSS feeds, sitemaps and topic lists, which only feed the index of known topics.
"""
import gzip
import html
import json
import os
import re
import shutil
import sys
from collections import defaultdict
from email.utils import parsedate_to_datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from net import read_manifest  # noqa: E402
from bs4 import BeautifulSoup

TOPIC_URL = re.compile(r"(?:forum\.tecommons\.org)?/t/(?:([^/?#]+)/)?(\d+)(?:/(\d+))?")


def body(raw, sha):
    with gzip.open(os.path.join(raw, "bodies", sha[:2], sha + ".gz"), "rb") as f:
        return f.read().decode("utf-8", errors="replace")


def iso(ts):
    """Wayback 14-digit timestamp -> ISO 8601."""
    return f"{ts[0:4]}-{ts[4:6]}-{ts[6:8]}T{ts[8:10]}:{ts[10:12]}:{ts[12:14]}Z"


def topic_ref(url):
    m = TOPIC_URL.search(url)
    if not m:
        return None
    slug, tid, n = m.groups()
    if slug and slug.isdigit() and not n:  # /t/<id>/<post_number>
        slug, tid, n = None, slug, tid
    return slug, int(tid), int(n) if n else None


class Store:
    def __init__(self):
        self.topics = {}                    # id -> topic meta (merged)
        self.posts = defaultdict(dict)      # id -> post_number -> post
        self.captures = defaultdict(list)   # id -> [capture refs that yielded posts]
        self.known = {}                     # id -> {title, slug, category_id, posts_count, ...}
        self.categories = {}                # id -> {name, slug, parent_category_id, color, description}
        self.crumb_cats = {}                # id -> category learnt from breadcrumbs (pre-2023 pages)

    # -- topic metadata ------------------------------------------------------------------
    def know(self, tid, newer, **fields):
        k = self.known.setdefault(tid, {"id": tid})
        for key, val in fields.items():
            if val in (None, "", []):
                continue
            stamp = k.get("_t_" + key, "")
            if key not in k or newer >= stamp:
                k[key] = val
                k["_t_" + key] = newer

    def add_post(self, tid, post, cap):
        # an empty body is a moderator "small action" (pinned, closed, moved...), not lost text
        if not BeautifulSoup(post["cooked"], "html.parser").get_text(strip=True) and \
                not re.search(r"<img|<iframe|<a ", post["cooked"]):
            post["action"] = post.get("action") or "moderator action"
        for k in ("created_at", "updated_at"):
            if post.get(k):
                post[k] = re.sub(r"\.\d+Z$", "Z", post[k])
        cur = self.posts[tid].get(post["post_number"])
        rank = (cap["timestamp"], post.get("_rich", 0))
        if cur is None or rank >= (cur["source"]["timestamp"], cur.get("_rich", 0)):
            post["source"] = cap
            self.posts[tid][post["post_number"]] = post

    # -- shapes ----------------------------------------------------------------------------
    def from_preloaded(self, doc, cap):
        el = re.search(r'data-preloaded="([^"]*)"', doc)
        if not el:
            return False
        try:
            pre = json.loads(html.unescape(el.group(1)))
        except json.JSONDecodeError:
            return False
        got = False
        ts = cap["timestamp"]
        if "site" in pre:
            try:
                for c in json.loads(pre["site"]).get("categories", []):
                    self.categories[c["id"]] = {k: c.get(k) for k in
                                                ("id", "name", "slug", "parent_category_id", "color", "description_text", "position")}
            except (json.JSONDecodeError, TypeError):
                pass
        for key, val in pre.items():
            try:
                data = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                continue
            if key.startswith("topic_") and isinstance(data, dict) and "post_stream" in data:
                got |= self.from_topic_json(data, cap)
            if isinstance(data, dict) and "topic_list" in data:
                for t in data["topic_list"].get("topics", []):
                    self.know(t["id"], ts, title=t.get("title"), slug=t.get("slug"), category_id=t.get("category_id"),
                              posts_count=t.get("posts_count"), created_at=t.get("created_at"),
                              last_posted_at=t.get("last_posted_at"), tags=_tags(t.get("tags")))
        return got

    def from_topic_json(self, t, cap):
        tid, ts = t["id"], cap["timestamp"]
        self.know(tid, ts, title=t.get("title"), slug=t.get("slug"), category_id=t.get("category_id"),
                  posts_count=t.get("posts_count"), created_at=t.get("created_at"), views=t.get("views"),
                  last_posted_at=t.get("last_posted_at"), tags=_tags(t.get("tags")),
                  highest_post_number=t.get("highest_post_number"), closed=t.get("closed"), archived=t.get("archived"))
        for s in t.get("suggested_topics", []) or []:
            self.know(s["id"], ts, title=s.get("title"), slug=s.get("slug"), category_id=s.get("category_id"),
                      posts_count=s.get("posts_count"), created_at=s.get("created_at"), last_posted_at=s.get("last_posted_at"))
        n = 0
        for p in t["post_stream"].get("posts", []):
            if p.get("hidden") or p.get("deleted_at"):
                continue
            likes = next((a.get("count", 0) for a in p.get("actions_summary", []) if a.get("id") == 2), 0)
            self.add_post(tid, {
                "post_number": p["post_number"], "username": p.get("username"), "name": p.get("name") or None,
                "created_at": p.get("created_at"), "updated_at": p.get("updated_at"),
                "reply_to_post_number": p.get("reply_to_post_number"), "like_count": likes,
                "cooked": p.get("cooked") or "", "action": p.get("action_code") if p.get("post_type") == 3 else None,
                "_rich": 1,
            }, cap)
            n += 1
        return n > 0

    def from_crawler(self, doc, cap, ref):
        soup = BeautifulSoup(doc, "lxml")
        posts = [d for d in soup.select("div.crawler-post") if d.select_one("div.post")]
        slug, tid, _ = ref
        ts = cap["timestamp"]
        canon = soup.select_one("link[rel=canonical]")
        if canon and canon.get("href"):
            r = topic_ref(canon["href"])
            if r:
                slug, tid = r[0] or slug, r[1]
        title_el = soup.select_one("#topic-title h1 a, h1 a[href*='/t/'], #topic-title h1, h1")
        og = soup.select_one("meta[property='og:title']")
        title = title_el.get_text(" ", strip=True) if title_el else (og["content"] if og else None)
        crumbs = soup.select("[itemprop=itemListElement] a[href*='/c/'], #breadcrumbs a[href*='/c/']")
        cat_id, parent = None, None
        for a in crumbs:  # /c/[<parent>/]<slug>/<id>
            m = re.search(r"/c/(?:[^/]+/)*?([^/]+)/(\d+)/?$", a["href"])
            name_el = a.select_one(".category-name, [itemprop=name]") or a
            name = name_el.get_text(strip=True)
            if m and name and ref[1] != -1:
                cid = int(m.group(2))
                self.crumb_cats.setdefault(cid, {"id": cid, "name": name, "slug": m.group(1),
                                                 "parent_category_id": parent})
                parent = cat_id = cid
        tags = [a.get_text(strip=True) for a in soup.select("a.discourse-tag")]
        self.know(tid, ts, title=title, slug=slug, category_id=cat_id, tags=tags)
        n = 0
        self.page_nums = []
        for d in posts:
            pos = d.select_one("[itemprop=position]")
            num = None
            if d.get("id", "").startswith("post_"):
                num = int(d["id"][5:])
            elif pos and pos.get_text(strip=True).lstrip("#").isdigit():
                num = int(pos.get_text(strip=True).lstrip("#"))
            if num is None:
                continue
            who = d.select_one(".creator [itemprop=name]")
            pub = d.select_one("[itemprop=datePublished]")
            mod = d.select_one("[itemprop=dateModified]")
            likes = 0
            for st in d.select("[itemprop=interactionStatistic]"):
                kind = st.select_one("[itemprop=interactionType]")
                cnt = st.select_one("[itemprop=userInteractionCount]")
                if kind and "Like" in (kind.get("content") or "") and cnt:
                    likes = int(cnt.get("content") or 0)
            self.add_post(tid, {
                "post_number": num, "username": who.get_text(strip=True) if who else None, "name": None,
                "created_at": (pub.get("datetime") or pub.get("content")) if pub else None,
                "updated_at": (mod.get("content") or mod.get("datetime")) if mod else None,
                "reply_to_post_number": None, "like_count": likes,
                "cooked": d.select_one("div.post").decode_contents().strip(), "action": None,
            }, cap)
            self.page_nums.append(num)
            n += 1
        # topic lists (category pages, /latest) in crawler form
        for a in soup.select("a.raw-topic-link, a.title.raw-link, td.main-link a[href*='/t/']"):
            r = topic_ref(a.get("href", ""))
            if r:
                row = a.find_parent("tr")
                cnt = row.select_one(".posts, td.replies span") if row else None
                n_posts = int(cnt.get_text(strip=True)) if cnt and cnt.get_text(strip=True).isdigit() else None
                self.know(r[1], ts, title=a.get_text(" ", strip=True), slug=r[0], posts_count=n_posts)
        # crawler view pages 20 posts at a time; a "next" link means replies past this page existed
        if n and soup.select_one("a[rel=next][href*='page=']"):
            self.know(tid, ts, more_after=max(int(k) for k in self.page_nums))
        return n > 0

    def from_rss(self, doc, cap):
        soup = BeautifulSoup(doc, "xml")
        n = 0
        for it in soup.find_all("item"):
            link = it.find("link")
            r = topic_ref(link.get_text() if link else "")
            if not r:
                continue
            slug, tid, num = r
            creator = it.find("dc:creator") or it.find("creator")
            date = it.find("pubDate")
            desc = it.find("description")
            if num is None or not desc:
                continue
            cooked = desc.get_text()
            # RSS descriptions append "N posts - M participants / Read full topic" footers
            cooked = re.sub(r"<p><small>\d+ posts? - \d+ participants?</small></p>\s*<p><a href=.*?Read full topic</a></p>\s*$", "", cooked, flags=re.S)
            self.add_post(tid, {
                "post_number": num, "username": (creator.get_text(strip=True).lstrip("@") if creator else None), "name": None,
                "created_at": parsedate_to_datetime(date.get_text()).isoformat().replace("+00:00", "Z") if date else None,
                "updated_at": None, "reply_to_post_number": None, "like_count": None, "cooked": cooked.strip(), "action": None,
            }, cap)
            n += 1
        return n > 0

    def from_sitemap(self, doc, cap):
        for loc in re.finditer(r"<loc>([^<]+)</loc>\s*(?:<lastmod>([^<]+)</lastmod>)?", doc):
            r = topic_ref(loc.group(1))
            if r:
                self.know(r[1], cap["timestamp"], slug=r[0], sitemap_lastmod=loc.group(2), in_sitemap=True)


# link-spam that landed on the forum after moderation stopped (cracked software, "free download")
SPAM_TITLE = re.compile(r"\b(crack(ed)?|keygen|serial key|license key|activation (key|code)|product key|"
                        r"full version|mod apk|apk download|free download|torrent|patch \+|portable \d)\b", re.I)


def is_spam(meta, posts):
    title = meta.get("title") or ""
    if SPAM_TITLE.search(title):
        return True
    first = posts[0]["cooked"] if posts else ""
    return bool(re.search(r"\bcrack(ed)?\b.*\b(download|key)\b", first[:2000], re.I | re.S)) and len(posts) <= 2


def _tags(tags):
    if not tags:
        return None
    return [t["name"] if isinstance(t, dict) else t for t in tags]


def main(raw, out):
    st = Store()
    rows = read_manifest(raw)
    rows = [r for r in rows if r["status"] == 200 and r["sha256"] and r["kind"] in ("topic", "listing")]
    rows.sort(key=lambda r: r["timestamp"])
    for r in rows:
        doc = body(raw, r["sha256"])
        cap = {"source": r["source"], "timestamp": r["timestamp"], "url": r["url"]}
        if r["kind"] == "listing":
            if "sitemap" in r["url"]:
                st.from_sitemap(doc, cap)
            elif doc.lstrip().startswith("<"):
                st.from_preloaded(doc, cap)
                st.from_crawler(doc, cap, (None, -1, None))
            continue
        ref = topic_ref(r["url"])
        if not ref:
            continue
        if ".rss" in r["url"] or "<rss" in doc[:500]:
            ok = st.from_rss(doc, cap)
        elif doc.lstrip().startswith("{"):
            try:
                ok = st.from_topic_json(json.loads(doc), cap)
            except (json.JSONDecodeError, KeyError):
                ok = False
        else:
            ok = st.from_preloaded(doc, cap)
            ok = st.from_crawler(doc, cap, ref) or ok
        if ok:
            st.captures[ref[1]].append(cap)
    st.known.pop(-1, None)

    # categories that the site JSON (2023+) never listed, e.g. deleted ones, come from breadcrumbs
    for cid, c in st.crumb_cats.items():
        st.categories.setdefault(cid, c)

    shutil.rmtree(os.path.join(out, "topics"), ignore_errors=True)  # derived output: rebuild whole
    os.makedirs(os.path.join(out, "topics"))
    index, spam = [], []
    for tid, k in sorted(st.known.items()):
        meta = {key: v for key, v in k.items() if not key.startswith("_t_")}
        posts = [{key: v for key, v in p.items() if key != "_rich"} for _, p in sorted(st.posts.get(tid, {}).items())]
        if is_spam(meta, posts):
            spam.append({"id": tid, "title": meta.get("title"), "slug": meta.get("slug"),
                         "username": posts[0]["username"] if posts else None})
            continue
        meta["total_known"] = bool(meta.get("posts_count") or meta.get("highest_post_number"))
        meta["archived_posts"] = sum(1 for p in posts if not p.get("action"))
        meta["archived_numbers"] = len(posts)
        if meta["archived_posts"]:
            meta.setdefault("created_at", posts[0]["created_at"])
            meta["last_archived_post_at"] = max((p["created_at"] or "") for p in posts) or None
            first, last = st.captures[tid][0]["timestamp"], st.captures[tid][-1]["timestamp"]
            meta["captured_between"] = [iso(first), iso(last)]
            with open(os.path.join(out, "topics", f"{tid}.json"), "w") as f:
                json.dump({**meta, "posts": posts, "captures": st.captures[tid]}, f, indent=1, ensure_ascii=False)
        index.append(meta)
    with open(os.path.join(out, "index.json"), "w") as f:
        json.dump(index, f, indent=1, ensure_ascii=False)
    with open(os.path.join(out, "excluded-spam.json"), "w") as f:
        json.dump(spam, f, indent=1, ensure_ascii=False)
    with open(os.path.join(out, "categories.json"), "w") as f:
        json.dump(sorted(st.categories.values(), key=lambda c: (c.get("position") or 999, c["name"])), f, indent=1, ensure_ascii=False)
    have = sum(1 for m in index if m["archived_posts"])
    print(f"known topics {len(index)}, with archived posts {have}, posts {sum(m['archived_posts'] for m in index)}, "
          f"categories {len(st.categories)}, spam excluded {len(spam)}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
