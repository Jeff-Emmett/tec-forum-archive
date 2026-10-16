"""Stage 4: render the merged records as a static site.

    python3 -I scraper/build.py data raw site

URLs mirror the original forum's, so an old link only needs its host swapped:
    forum.tecommons.org/t/<slug>/<id>  ->  tec.mycostack.xyz/t/<slug>/<id>/
Archived HTML is untrusted third-party content: it is sanitised to an allowlist here,
and nginx serves the site under a CSP that forbids inline script as a second line.
"""
import datetime
import glob
import gzip
import json
import os
import re
import shutil
import sys
import urllib.parse
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from net import read_manifest  # noqa: E402
import emoji
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader, select_autoescape

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "site_src")
FORUM = "https://forum.tecommons.org"
WAYBACK = "https://web.archive.org/web/2026/"
UPLOAD_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".avif", ".ico"}
DROP_TAGS = ["script", "style", "form", "input", "button", "textarea", "select", "object", "embed",
             "link", "meta", "noscript", "svg", "template", "base", "frame", "frameset"]
KEEP_ATTRS = {"href", "src", "alt", "title", "width", "height", "colspan", "rowspan", "class", "id",
              "start", "type", "open", "datetime", "lang", "dir", "align"}
TOPIC_URL = re.compile(r"^/t/(?:([^/?#]+)/)?(\d+)(?:/(\d+))?/?$")


def wb(path, image=False):
    return f"https://web.archive.org/web/2026{'im_' if image else ''}/{FORUM}{path}"


class Site:
    def __init__(self, data, raw, out):
        self.out, self.data = out, data
        self.user_keys = set()
        self.index = json.load(open(os.path.join(data, "index.json")))
        self.cats = {c["id"]: c for c in json.load(open(os.path.join(data, "categories.json")))}
        self.topics = {}
        for fn in glob.glob(os.path.join(data, "topics", "*.json")):
            t = json.load(open(fn))
            self.topics[t["id"]] = t
        self.meta = {m["id"]: m for m in self.index}
        self.uploads = {}  # forum path -> sha256
        for r in read_manifest(raw):
            if r["kind"] == "upload" and r["status"] == 200 and r["sha256"]:
                p = urllib.parse.urlsplit(r["url"]).path
                if os.path.splitext(p)[1].lower() in UPLOAD_EXT:
                    self.uploads[p] = r["sha256"]
        self.raw = raw
        self.users = defaultdict(list)
        self.env = Environment(loader=FileSystemLoader(os.path.join(SRC, "templates")),
                               autoescape=select_autoescape(["html"]), trim_blocks=True, lstrip_blocks=True)
        self.env.filters["date"] = fmt_date
        self.env.filters["year"] = lambda s: (s or "")[:4]

    # -- helpers ---------------------------------------------------------------------------
    def topic_path(self, tid):
        m = self.meta.get(tid, {})
        return f"/t/{m.get('slug') or 'topic'}/{tid}/"

    def category(self, m):
        c = self.cats.get(m.get("category_id"))
        if not c:
            return None
        parent = self.cats.get(c.get("parent_category_id"))
        return {"name": c["name"], "slug": c["slug"], "path": f"/c/{c['slug']}/",
                "parent": parent["name"] if parent else None}

    def local_url(self, href, image=False):
        """Map a forum URL onto this archive, else onto the Wayback Machine."""
        u = urllib.parse.urlsplit(href)
        if u.scheme in ("javascript", "data", "vbscript", "file"):
            return None
        if u.netloc and u.netloc.lower() not in ("forum.tecommons.org", "www.forum.tecommons.org"):
            return href if u.scheme in ("http", "https", "mailto", "") else None
        if not u.netloc and not u.path.startswith("/"):
            return href  # fragment or relative
        path, frag = u.path, (f"#{u.fragment}" if u.fragment else "")
        m = TOPIC_URL.match(path)
        if m:
            tid, n = int(m.group(2)), m.group(3)
            if tid in self.topics:
                return self.topic_path(tid) + (f"#post_{n}" if n else frag)
            return wb(path)
        if path in self.uploads:
            return path
        if path.startswith("/uploads/") and "/optimized/" in path:
            return wb(path, image)
        cm = re.match(r"^/c/(?:[^/]+/)*?([a-z0-9-]+)(?:/\d+)?/?$", path)
        if cm and any(c["slug"] == cm.group(1) for c in self.cats.values()):
            return f"/c/{cm.group(1)}/"
        um = re.match(r"^/u/([^/]+)/?", path)
        if um and um.group(1).lower() in self.user_keys:
            return f"/u/{um.group(1).lower()}/"
        return wb(path + (f"?{u.query}" if u.query else ""), image)

    def clean(self, cooked):
        soup = BeautifulSoup(cooked, "html.parser")
        for t in soup(DROP_TAGS):
            t.decompose()
        for f in soup("iframe"):
            src = self.local_url(f.get("src", "")) if f.get("src") else None
            if src and src.startswith("http"):
                a = soup.new_tag("a", href=src)
                a.string = f"Embedded content: {src}"
                f.replace_with(a)
            else:
                f.decompose()
        for img in soup("img"):
            cls = img.get("class") or []
            if "avatar" in cls:
                img.decompose()
                continue
            src = img.get("src", "")
            if src.startswith("//"):
                src = "https:" + src
            if "emoji" in cls:
                code = img.get("title") or img.get("alt") or ""
                char = to_unicode(code)
                path = urllib.parse.urlsplit(src).path
                if char:
                    img.replace_with(char)
                elif path in self.uploads:
                    img["src"] = path
                else:
                    img.replace_with(code)
                continue
            new = self.local_url(src, image=True) if src else None
            if new:
                img["src"] = new
                img["loading"] = "lazy"
            else:
                img.decompose()
        for a in soup("a"):
            href = a.get("href")
            if href is None:
                continue
            if href.startswith("//"):
                href = "https:" + href
            new = self.local_url(href)
            if new is None:
                del a["href"]
            else:
                a["href"] = new
                if new.startswith("http"):
                    a["rel"] = "nofollow noopener"
        for t in soup.find_all(True):
            for attr in list(t.attrs):
                if attr not in KEEP_ATTRS and not (t.name == "img" and attr == "loading") \
                        and not (t.name == "a" and attr == "rel"):
                    del t[attr]
            for attr in ("href", "src"):
                if t.get(attr, "").strip().lower().startswith(("javascript:", "data:", "vbscript:")):
                    del t[attr]
            if t.get("id"):
                t["id"] = "c-" + t["id"]  # keep cooked anchors from colliding with page ids
        return str(soup)

    def render(self, rel, template, **ctx):
        path = os.path.join(self.out, rel.strip("/"), "index.html") if not rel.endswith(".html") \
            else os.path.join(self.out, rel.strip("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(self.env.get_template(template).render(**ctx, site=self.summary))

    # -- build -----------------------------------------------------------------------------
    def build(self):
        if os.path.exists(self.out):
            shutil.rmtree(self.out)
        os.makedirs(self.out)
        shutil.copytree(os.path.join(SRC, "assets"), os.path.join(self.out, "assets"))
        for p, sha in self.uploads.items():
            dest = os.path.join(self.out, p.lstrip("/"))
            if ".." in p:
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with gzip.open(os.path.join(self.raw, "bodies", sha[:2], sha + ".gz"), "rb") as f, open(dest, "wb") as g:
                g.write(f.read())

        names = {}
        for t in self.topics.values():
            for p in t["posts"]:
                if p.get("username"):
                    key = p["username"].lower()
                    names.setdefault(key, p["username"])
                    if p.get("name"):
                        names[key + "\0name"] = p["name"]
        self.user_keys = {k for k in names if "\0" not in k}

        rows = []
        for m in self.index:
            t = self.topics.get(m["id"])
            have = m.get("archived_numbers", m["archived_posts"])
            total = topic_total(m, have)
            row = {
                "id": m["id"], "title": m.get("title") or (m.get("slug") or f"Topic {m['id']}").replace("-", " ").capitalize(),
                "path": self.topic_path(m["id"]) if t else wb(f"/t/{m.get('slug') or 'topic'}/{m['id']}"),
                "archived": bool(t), "category": self.category(m), "tags": m.get("tags") or [],
                "created_at": m.get("created_at"), "last": m.get("last_posted_at") or m.get("last_archived_post_at") or m.get("created_at"),
                "have": have, "posts": m["archived_posts"], "total": total or None, "total_known": m.get("total_known"),
                "more": bool(t) and total <= have and (m.get("more_after") or 0) >= t["posts"][-1]["post_number"],
            }
            rows.append(row)
        rows.sort(key=lambda r: r["created_at"] or "", reverse=True)
        archived = [r for r in rows if r["archived"]]
        n_posts = sum(r["posts"] for r in archived)
        captures = {(c["source"], c["timestamp"], c["url"]) for t in self.topics.values() for c in t["captures"]}
        dates = sorted(p["created_at"] for t in self.topics.values() for p in t["posts"] if p.get("created_at"))
        complete = sum(1 for r in archived if r["total_known"] and r["have"] >= r["total"] and not r["more"])
        self.summary = {
            "topics": len(archived), "known": len(rows), "posts": n_posts, "captures": len(captures),
            "complete": complete, "people": len(self.user_keys),
            "spam": len(json.load(open(os.path.join(self.data, "excluded-spam.json")))),
            "first": dates[0] if dates else None, "last": dates[-1] if dates else None,
            "built": datetime.date.today().isoformat(),
            "wayback_captures": sum(1 for c in captures if c[0] == "wayback"),
            "cc_captures": sum(1 for c in captures if c[0] == "commoncrawl"),
        }

        # topics
        for tid, t in self.topics.items():
            m = self.meta[tid]
            posts, prev = [], 0
            total = topic_total(m, len(t["posts"]))
            more = (m.get("more_after") or 0) >= (t["posts"][-1]["post_number"] if t["posts"] else 0)
            for p in t["posts"]:
                n = p["post_number"]
                if n > prev + 1:
                    posts.append({"gap": (prev + 1, n - 1)})
                prev = n
                user = (p.get("username") or "").lower()
                display = names.get(user + "\0name")
                if user and not p.get("action"):
                    self.users[user].append({"topic": tid, "n": n, "created_at": p.get("created_at")})
                posts.append({**p, "display": display, "user": user, "html": self.clean(p["cooked"]),
                              "source_url": src_url(p["source"])})
            if total > prev:
                posts.append({"gap": (prev + 1, total), "tail": True})
            elif more:
                posts.append({"gap": (prev + 1, None), "tail": True})
            row = next(r for r in rows if r["id"] == tid)
            self.render(self.topic_path(tid), "topic.html", t=row, posts=posts, total=total,
                        captures=sorted(t["captures"], key=lambda c: c["timestamp"]),
                        original=f"{FORUM}/t/{m.get('slug') or 'topic'}/{tid}", src_url=src_url)
            self.render(f"/t/{tid}/", "redirect.html", to=self.topic_path(tid), title=row["title"])

        # categories
        by_cat = defaultdict(list)
        for r in rows:
            if r["category"]:
                by_cat[r["category"]["slug"]].append(r)
        cats = []
        for c in self.cats.values():
            if by_cat.get(c["slug"]):
                parent = self.cats.get(c.get("parent_category_id"))
                cats.append({"name": c["name"], "slug": c["slug"], "parent": parent["name"] if parent else None,
                             "description": c.get("description_text"), "rows": by_cat[c["slug"]],
                             "archived": sum(1 for r in by_cat[c["slug"]] if r["archived"])})
        cats.sort(key=lambda c: (-c["archived"], -len(c["rows"])))
        # the forum later folded old categories under "Archive", leaving repeated names
        bare = Counter(re.sub(r"^\W+", "", c["name"]).lower() for c in cats)
        for c in cats:
            c["label"] = f"{c['parent']} › {c['name']}" if c["parent"] and bare[re.sub(r"^\W+", "", c["name"]).lower()] > 1 else c["name"]
        for c in cats:
            self.render(f"/c/{c['slug']}/", "category.html", c=c)
        uncategorised = [r for r in rows if not r["category"]]

        # people
        titles = {r["id"]: r for r in rows}
        people = []
        for user, items in self.users.items():
            items.sort(key=lambda i: i["created_at"] or "")
            for i in items:
                i["title"] = titles[i["topic"]]["title"]
                i["path"] = self.topic_path(i["topic"]) + f"#post_{i['n']}"
            people.append({"user": user, "username": names[user], "display": names.get(user + "\0name"),
                           "count": len(items), "first": items[0]["created_at"], "last": items[-1]["created_at"]})
            self.render(f"/u/{user}/", "user.html", u=people[-1], items=items)
        people.sort(key=lambda p: -p["count"])

        years = defaultdict(list)
        for r in archived:
            years[(r["created_at"] or "????")[:4]].append(r)
        self.render("/", "index.html", years=sorted(years.items(), reverse=True), cats=cats,
                    uncategorised=[r for r in uncategorised if r["archived"]])
        self.render("/categories/", "categories.html", cats=cats)
        self.render("/people/", "people.html", people=people)
        self.render("/missing/", "missing.html", rows=[r for r in rows if not r["archived"]],
                    partial=sorted([r for r in archived if r["more"] or (r["total"] and r["have"] < r["total"])],
                                   key=lambda r: r["have"] - r["total"]))
        self.render("/about/", "about.html")
        self.render("/search/", "search.html")
        self.render("404.html", "404.html")

        # downloads: the merged corpus, as data
        os.makedirs(os.path.join(self.out, "data"), exist_ok=True)
        with gzip.open(os.path.join(self.out, "data", "tec-forum-archive.jsonl.gz"), "wt") as f:
            for tid in sorted(self.topics):
                f.write(json.dumps(self.topics[tid], ensure_ascii=False) + "\n")
        for fn in ("index.json", "categories.json", "excluded-spam.json"):
            shutil.copy(os.path.join(self.data, fn), os.path.join(self.out, "data", fn))
        with open(os.path.join(self.out, "robots.txt"), "w") as f:
            f.write("User-agent: *\nAllow: /\n")
        print(f"built {len(self.topics)} topics, {n_posts} posts, {len(people)} people, "
              f"{len(cats)} categories, {len(self.uploads)} uploads -> {self.out}")


# Discourse's emoji names that differ from the GitHub/Slack aliases the emoji package knows
DISCOURSE_EMOJI = {"slight_smile": "🙂", "slightly_smiling_face": "🙂", "slight_frown": "🙁", "upside_down": "🙃",
                   "hugs": "🤗", "thinking": "🤔", "rofl": "🤣", "nerd": "🤓", "star_struck": "🤩",
                   "face_with_monocle": "🧐", "partying_face": "🥳", "pleading_face": "🥺", "exploding_head": "🤯"}


def to_unicode(code):
    name = code.strip(":").split(":t")[0]  # drop skin-tone suffix ":+1:t3:"
    if name in DISCOURSE_EMOJI:
        return DISCOURSE_EMOJI[name]
    char = emoji.emojize(f":{name}:", language="alias")
    return None if char.startswith(":") else char


def topic_total(m, have):
    """Best known post count. Discourse's posts_count skips deleted posts; highest_post_number doesn't."""
    return max(m.get("posts_count") or 0, m.get("highest_post_number") or 0, have)


def src_url(c):
    if c["source"] == "wayback":
        return f"https://web.archive.org/web/{c['timestamp']}/{c['url']}"
    return f"https://index.commoncrawl.org/ (capture {c['timestamp']}, {c['url']})"


def fmt_date(s, fmt="%-d %b %Y"):
    if not s:
        return "undated"
    try:
        return datetime.datetime.fromisoformat(s.replace("Z", "+00:00")).strftime(fmt)
    except ValueError:
        return s[:10]


if __name__ == "__main__":
    Site(sys.argv[1], sys.argv[2], sys.argv[3]).build()
