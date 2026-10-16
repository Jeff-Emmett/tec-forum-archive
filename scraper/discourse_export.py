"""Turn the merged archive into an import bundle for a real Discourse (discourse/import_tec.rb).

    discourse_export.py data raw out

out/bundle.json   categories, users, topics with posts (raw = Discourse-flavoured HTML/BBCode)
out/uploads/      every recovered upload, named <sha256><ext>

Post bodies go through the same sanitiser and link rewriting as the static site (build.Site.clean),
after Discourse-specific structure is turned back into what Discourse's own markdown expects:
quotes -> [quote] BBCode, oneboxes -> a bare link line (Discourse re-oneboxes it), lightboxes ->
the original image, mentions -> @name, polls -> a static list (results were rarely captured, and a
live [poll] would start empty). Topic, category and post numbers are the forum's own, so old URLs
keep working once the host is swapped.
"""
import gzip
import json
import os
import re
import shutil
import sys
import urllib.parse

from bs4 import BeautifulSoup, NavigableString

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build import Site, TOPIC_URL, wb  # noqa: E402

MARK = "⁣TECMARK{}⁣"  # invisible-separator-wrapped token survives clean() as text


def plain_actions(posts):
    """Small actions Discourse has its own codes for; anything else is skipped."""
    return {"pinned.enabled", "pinned.disabled", "closed.enabled", "closed.disabled",
            "visible.enabled", "visible.disabled", "autobumped", "archived.enabled",
            "pinned_globally.enabled", "pinned_globally.disabled"}


class Exporter:
    def __init__(self, data, raw, out):
        self.site = Site(data, raw, os.path.join(out, ".unused"))
        self.out = out
        self.marks = []
        names = set()
        for t in self.site.topics.values():
            for p in t["posts"]:
                if p.get("username"):
                    names.add(p["username"].lower())
        self.site.user_keys = names

    def mark(self, text):
        self.marks.append(text)
        return MARK.format(len(self.marks) - 1)

    def pre(self, cooked, topic_id):
        """Discourse structure -> markers / simpler HTML, before the shared sanitiser runs."""
        soup = BeautifulSoup(cooked, "html.parser")
        for q in soup.select("aside.quote"):
            user = q.get("data-username")
            post, topic = q.get("data-post"), q.get("data-topic")
            attrs = ", ".join(x for x in [user, post and f"post:{post}", topic and f"topic:{topic}"] if x)
            bq = q.find("blockquote")
            inner = "".join(str(c) for c in (bq.contents if bq else []))
            open_ = self.mark(f'[quote="{attrs}"]' if attrs else "[quote]")
            close = self.mark("[/quote]")
            q.replace_with(BeautifulSoup(f"<p>{open_}</p>{inner}<p>{close}</p>", "html.parser"))
        for ob in soup.select("aside.onebox, div.onebox"):
            link = ob.get("data-onebox-src") or (ob.select_one("header a[href]") or ob.select_one("a[href]") or {}).get("href")
            if link:
                p = soup.new_tag("p")
                a = soup.new_tag("a", href=link)
                a.string = link
                p.append(a)
                ob.replace_with(p)
            else:
                ob.decompose()
        for lb in soup.select("div.lightbox-wrapper"):
            a, img = lb.find("a"), lb.find("img")
            if img is None:
                lb.decompose()
                continue
            src = (a.get("href") if a and a.get("href") else None) or img.get("src")
            new = soup.new_tag("img", src=src, alt=img.get("alt", ""))
            for k in ("width", "height"):
                if img.get(k):
                    new[k] = img[k]
            lb.replace_with(new)
        for m in soup.select("a.mention"):
            m.replace_with(NavigableString(m.get_text()))
        for m in soup.select("a.mention-group"):
            m.replace_with(NavigableString(m.get_text()))
        for poll in soup.select("div.poll"):
            opts = [li.get_text(" ", strip=True) for li in poll.select("li[data-poll-option-id]")]
            opts = [o for o in opts if o.lower() != "see results"]
            voters = poll.select_one(".info-number")
            note = "Poll (results as captured: " + (voters.get_text(strip=True) + " voters" if voters else "not captured") + ")"
            frag = f"<p><em>{note}</em></p><ul>" + "".join(f"<li>{BeautifulSoup(o, 'html.parser').get_text()}</li>" for o in opts) + "</ul>"
            poll.replace_with(BeautifulSoup(frag, "html.parser"))
        for el in soup.select("div.meta, span.filename, span.informations, span.expand, div.quote-controls"):
            el.decompose()
        return str(soup)

    def to_raw(self, cooked, topic_id):
        html = self.site.clean(self.pre(cooked or "", topic_id))
        # static-site paths -> Discourse paths
        html = re.sub(r'(/t/[^/"#?]+/\d+)/#post_(\d+)', r"\1/\2", html)
        html = re.sub(r'href="(/t/[^/"#?]+/\d+)/"', r'href="\1"', html)
        html = re.sub(r'href="/c/([^"/]+)/"', r'href="/c/\1"', html)
        html = re.sub(r'href="/u/([^"/]+)/"', r'href="/u/\1"', html)
        html = html.replace(' loading="lazy"', "")
        # one HTML block per top-level element: a blank line inside a block would end it and
        # hand the rest to the markdown parser
        soup = BeautifulSoup(html, "html.parser")
        blocks = []
        for node in soup.contents:
            s = str(node)
            if isinstance(node, NavigableString):
                s = s.strip()
                if not s:
                    continue
            elif node.name != "pre":
                s = re.sub(r"\n\s*\n", "\n", s).strip()
            blocks.append(s)
        raw = "\n\n".join(blocks)

        def unmark(m):
            return self.marks[int(m.group(1))]
        raw = re.sub(r"<p>⁣TECMARK(\d+)⁣</p>", lambda m: unmark(m), raw)
        raw = re.sub(r"⁣TECMARK(\d+)⁣", lambda m: unmark(m), raw)
        return raw.strip() or "<p><em>(empty post)</em></p>"

    def run(self):
        os.makedirs(os.path.join(self.out, "uploads"), exist_ok=True)
        uploads = {}
        for path, sha in self.site.uploads.items():
            if ".." in path:
                continue
            name = sha + os.path.splitext(path)[1].lower()
            dest = os.path.join(self.out, "uploads", name)
            if not os.path.exists(dest):
                with gzip.open(os.path.join(self.site.raw, "bodies", sha[:2], sha + ".gz"), "rb") as f, open(dest, "wb") as g:
                    shutil.copyfileobj(f, g)
            uploads[path] = {"file": name, "name": os.path.basename(path)}

        users, topics = {}, []
        codes = plain_actions(None)
        for tid in sorted(self.site.topics):
            t = self.site.topics[tid]
            m = self.site.meta.get(tid, {})
            posts, seen = [], set()
            for p in sorted(t["posts"], key=lambda p: int(p["post_number"])):
                n = int(p["post_number"])
                seen.add(n)
                u = p.get("username") or "system"
                if u != "system":
                    rec = users.setdefault(u.lower(), {"username": u, "name": None, "first_seen": p["created_at"]})
                    rec["name"] = rec["name"] or p.get("name")
                    rec["first_seen"] = min(rec["first_seen"], p["created_at"])
                act = p.get("action")
                if act:
                    if act in codes:
                        posts.append({"post_number": n, "username": u, "created_at": p["created_at"], "action_code": act})
                    continue  # empty moderator notes: number stays, nothing invented
                posts.append({
                    "post_number": n, "username": u, "created_at": p["created_at"],
                    "updated_at": p.get("updated_at"), "reply_to_post_number": p.get("reply_to_post_number"),
                    "like_count": p.get("like_count") or 0, "raw": self.to_raw(p.get("cooked"), tid),
                    "source": p.get("source"),
                })
            top = t.get("posts_count") if t.get("total_known") else None
            highest = max([top or 0, *seen]) if seen else 0
            gaps, run = [], None
            for n in range(1, highest + 1):
                if n not in seen:
                    run = [n, n] if run is None else [run[0], n]
                elif run:
                    gaps.append(run)
                    run = None
            if run:
                gaps.append(run)
            topics.append({
                "id": tid, "title": t["title"], "slug": t["slug"], "category_id": t.get("category_id"),
                "created_at": t.get("created_at") or (posts[0]["created_at"] if posts else None),
                "posts": posts, "gaps": gaps, "complete": bool(t.get("total_known") and not gaps),
                "captures": [{"source": c.get("source"), "timestamp": c.get("timestamp"), "url": c.get("url")}
                             for c in t.get("captures", [])],
            })
        missing = [{"id": m["id"], "slug": m.get("slug"), "title": m.get("title"),
                    "wayback": wb(f"/t/{m.get('slug') or 'topic'}/{m['id']}")}
                   for m in self.site.index if m["id"] not in self.site.topics and not m.get("spam")]
        cats = sorted(self.site.cats.values(), key=lambda c: (c.get("parent_category_id") is not None, c["id"]))
        bundle = {"categories": cats, "users": sorted(users.values(), key=lambda u: u["first_seen"]),
                  "topics": topics, "uploads": uploads, "missing": missing}
        with open(os.path.join(self.out, "bundle.json"), "w") as f:
            json.dump(bundle, f, ensure_ascii=False)
        nposts = sum(len(t["posts"]) for t in topics)
        print(f"{len(cats)} categories, {len(users)} users, {len(topics)} topics, {nposts} posts, "
              f"{len(uploads)} uploads, {len(missing)} missing -> {self.out}")


if __name__ == "__main__":
    Exporter(*sys.argv[1:4]).run()
