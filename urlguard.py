#!/usr/bin/env python3
"""
cms-migration-url-guard
=======================

Pre- and post-migration URL safety net for CMS migrations
(Joomla 3 -> Joomla 4/5/6, WordPress host moves, theme rebuilds).

Three commands, standard library only (no pip install needed):

  crawl   Crawl a live site (plus its sitemap) and build a URL inventory:
          status, redirects, title, canonical, Joomla component/view,
          image paths, legacy-extension URLs, template fingerprint.

  report  Turn a crawl folder into a readable Markdown audit
          (what exists, what will break, what needs a 301).

  diff    After the rebuild: replay every old URL against the new
          (staging) site and produce a pass/fail sheet plus a draft
          301 redirect list for .htaccess.

Examples:
  python3 urlguard.py crawl  https://www.example.com.au --out audit-old
  python3 urlguard.py report audit-old
  python3 urlguard.py diff   audit-old https://staging.example.com.au --out diff-staging
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

VERSION = "1.0.0"
UA = f"cms-migration-url-guard/{VERSION} (pre-migration audit)"

SKIP_EXT = re.compile(
    r"\.(jpe?g|png|gif|webp|svg|ico|bmp|tiff?|pdf|zip|rar|7z|gz|mp4|mp3|mov|avi|"
    r"woff2?|ttf|eot|otf|css|js|json|xml|txt|docx?|xlsx?|pptx?|csv)$",
    re.I,
)
IMG_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|bmp|tiff?)$", re.I)

# Query parameters that only create duplicate crawl paths.
NOISE_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "tmpl", "print", "format", "lang",
}

# Joomla components we classify explicitly. Key = option name.
LEGACY_OUT_OF_SCOPE = {
    "com_eshop": "EShop",
    "com_acym": "AcyMailing",
    "com_acymailing": "AcyMailing",
    "com_users": "User login / registration",
    "com_comprofiler": "Community Builder",
}

TEMPLATE_HINTS = [
    (re.compile(r"/plugins/system/t3/", re.I), "T3 framework"),
    (re.compile(r"/templates/([a-z0-9_\-]+)/", re.I), "template:{0}"),
    (re.compile(r"/media/com_osproperty/", re.I), "OS Property assets"),
    (re.compile(r"/components/com_osproperty/", re.I), "OS Property assets"),
    (re.compile(r"mootools", re.I), "MooTools (legacy JS)"),
    (re.compile(r"jquery[.-]?1\.", re.I), "jQuery 1.x"),
    (re.compile(r"bootstrap(\.min)?\.css|bootstrap/2", re.I), "Bootstrap CSS"),
    (re.compile(r"slick|owl\.carousel|revslider|sp-smart-slider|nivo", re.I), "Slider script"),
    (re.compile(r"popup|modal-?box|jbox|sexy", re.I), "Popup script"),
]


# --------------------------------------------------------------------------
# HTML parsing
# --------------------------------------------------------------------------

class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.images: list[str] = []
        self.assets: list[str] = []
        self.title = ""
        self.h1 = ""
        self.canonical = ""
        self.robots = ""
        self.generator = ""
        self.body_class = ""
        self._in_title = False
        self._in_h1 = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            self.links.append(a["href"])
        elif tag == "img":
            for key in ("src", "data-src", "data-original", "data-lazy-src"):
                if a.get(key):
                    self.images.append(a[key])
            if a.get("srcset"):
                for part in a["srcset"].split(","):
                    src = part.strip().split(" ")[0]
                    if src:
                        self.images.append(src)
        elif tag == "link":
            rel = a.get("rel", "").lower()
            if rel == "canonical":
                self.canonical = a.get("href", "")
            elif a.get("href"):
                self.assets.append(a["href"])
        elif tag == "script" and a.get("src"):
            self.assets.append(a["src"])
        elif tag == "meta":
            name = a.get("name", "").lower()
            if name == "robots":
                self.robots = a.get("content", "")
            elif name == "generator":
                self.generator = a.get("content", "")
            elif a.get("property", "").lower() == "og:image" and a.get("content"):
                self.images.append(a["content"])
        elif tag == "body":
            self.body_class = a.get("class", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "h1" and not self.h1:
            self._in_h1 = True
        # inline style background images
        style = a.get("style", "")
        if "url(" in style:
            for m in re.finditer(r"url\(['\"]?([^'\")]+)", style):
                self.images.append(m.group(1))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif self._in_h1:
            self.h1 += data


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


_opener_noredir = urllib.request.build_opener(NoRedirect)


def fetch(url: str, timeout: float, method: str = "GET", max_hops: int = 10):
    """Fetch following redirects manually so every hop is recorded.

    Returns dict(status, final_url, chain, headers, body)."""
    chain: list[tuple[int, str]] = []
    current = url
    for _ in range(max_hops):
        req = urllib.request.Request(current, method=method, headers={
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Encoding": "gzip",
        })
        try:
            resp = _opener_noredir.open(req, timeout=timeout)
            status = resp.status
            headers = dict(resp.headers.items())
            raw = resp.read() if method == "GET" else b""
        except urllib.error.HTTPError as e:
            status = e.code
            headers = dict(e.headers.items()) if e.headers else {}
            try:
                raw = e.read() if method == "GET" else b""
            except Exception:
                raw = b""
        except Exception as e:  # DNS, TLS, timeout
            return {"status": 0, "final_url": current, "chain": chain,
                    "headers": {}, "body": "", "error": str(e)[:200]}

        if status in (301, 302, 303, 307, 308):
            loc = headers.get("Location") or headers.get("location")
            chain.append((status, current))
            if not loc:
                break
            current = urllib.parse.urljoin(current, loc)
            continue

        if headers.get("Content-Encoding", headers.get("content-encoding", "")) == "gzip":
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        ctype = headers.get("Content-Type", headers.get("content-type", ""))
        charset = "utf-8"
        m = re.search(r"charset=([\w-]+)", ctype)
        if m:
            charset = m.group(1)
        try:
            body = raw.decode(charset, errors="replace")
        except LookupError:
            body = raw.decode("utf-8", errors="replace")
        return {"status": status, "final_url": current, "chain": chain,
                "headers": headers, "body": body, "error": ""}
    return {"status": -1, "final_url": current, "chain": chain,
            "headers": {}, "body": "", "error": "too many redirects"}


# --------------------------------------------------------------------------
# URL helpers
# --------------------------------------------------------------------------

def norm_host(h: str) -> str:
    h = h.lower()
    return h[4:] if h.startswith("www.") else h


def normalize(url: str, base: str) -> str | None:
    url = url.strip()
    if not url or url.startswith(("mailto:", "tel:", "javascript:", "#", "data:", "skype:", "whatsapp:")):
        return None
    absu = urllib.parse.urljoin(base, url)
    p = urllib.parse.urlsplit(absu)
    if p.scheme not in ("http", "https"):
        return None
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True)
         if k.lower() not in NOISE_PARAMS]
    path = p.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    return urllib.parse.urlunsplit((p.scheme, p.netloc.lower(), path, urllib.parse.urlencode(q), ""))


def dedupe_key(url: str) -> str:
    """Order-insensitive key so ?a=1&b=2 and ?b=2&a=1 are crawled once."""
    p = urllib.parse.urlsplit(url)
    q = sorted(urllib.parse.parse_qsl(p.query, keep_blank_values=True))
    return urllib.parse.urlunsplit(("", norm_host(p.netloc), p.path, urllib.parse.urlencode(q), ""))


def same_site(url: str, root_host: str) -> bool:
    return norm_host(urllib.parse.urlsplit(url).netloc) == root_host


def path_of(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return p.path + (("?" + p.query) if p.query else "")


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

def joomla_route(url: str, body_class: str) -> tuple[str, str]:
    """Best-effort (option, view) for a Joomla URL."""
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    option = q.get("option", "")
    view = q.get("view", "") or q.get("task", "")
    if not option and body_class:
        # T3 / many Joomla templates emit e.g. "com_osproperty view-property"
        m = re.search(r"\b(com_[a-z0-9_]+)\b", body_class)
        if m:
            option = m.group(1)
        m = re.search(r"\bview-([a-z0-9_]+)\b", body_class)
        if m:
            view = m.group(1)
    return option, view


def classify(url: str, option: str, view: str, title: str) -> str:
    p = urllib.parse.urlsplit(url)
    path = p.path.lower()
    q = p.query.lower()
    if path in ("/", "/index.php") and not q:
        return "home"
    if option in LEGACY_OUT_OF_SCOPE:
        return "out-of-scope:" + LEGACY_OUT_OF_SCOPE[option]
    if re.search(r"(^|/)(eshop|shop|cart|checkout)(/|$)", path):
        return "out-of-scope:EShop"
    if re.search(r"acym|acymailing|newsletter", path + q):
        return "out-of-scope:AcyMailing"
    if re.search(r"(^|/)(login|logout|register|registration|reset|remind|profile|my-account|user)(/|$)", path):
        return "out-of-scope:User login / registration"
    if re.search(r"(^|/)vip(-[a-z0-9-]+)?(/|$)", path) or "vip" in title.lower():
        return "out-of-scope:VIP area"
    if option == "com_osproperty" or "osproperty" in path + q:
        v = view or ""
        if v in ("property_details", "details", "property") or re.search(r"/\d+-[a-z0-9-]+(\.html)?$", path):
            return "osproperty:listing"
        if v in ("ltype", "lcategory", "category", "type"):
            return "osproperty:category"
        if v in ("lagents", "agentdetails", "agent", "lcompanies", "companydetails", "company"):
            return "osproperty:agent/company"
        if v in ("ladvsearch", "search", "advsearch") or "search" in path:
            return "osproperty:search"
        return "osproperty:other"
    if option == "com_content":
        if view in ("category", "categories", "featured", "archive"):
            return "content:category"
        return "content:article"
    if option == "com_contact":
        return "contact"
    if option == "com_search" or option == "com_finder" or "/search" in path:
        return "site-search"
    if re.search(r"[?&](start|limitstart|page)=", "?" + q):
        return "pagination"
    if option:
        return "component:" + option
    return "page"


# --------------------------------------------------------------------------
# crawl
# --------------------------------------------------------------------------

def read_sitemaps(root: str, timeout: float, limit: int = 20000) -> list[str]:
    found: list[str] = []
    candidates = [urllib.parse.urljoin(root, "/sitemap.xml"),
                  urllib.parse.urljoin(root, "/index.php?option=com_osmap&view=xml&id=1"),
                  urllib.parse.urljoin(root, "/index.php?option=com_jmap&view=sitemap&format=xml"),
                  urllib.parse.urljoin(root, "/index.php?option=com_xmap&view=xml&id=1")]
    rb = fetch(urllib.parse.urljoin(root, "/robots.txt"), timeout)
    if rb["status"] == 200:
        for line in rb["body"].splitlines():
            if line.lower().startswith("sitemap:"):
                candidates.insert(0, line.split(":", 1)[1].strip())
    seen = set()
    queue = deque(candidates)
    while queue and len(found) < limit:
        sm = queue.popleft()
        if sm in seen:
            continue
        seen.add(sm)
        r = fetch(sm, timeout)
        if r["status"] != 200 or "<loc>" not in r["body"]:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r["body"])
        if "<sitemapindex" in r["body"]:
            queue.extend(locs)
        else:
            found.extend(l.replace("&amp;", "&") for l in locs)
    return found


def cmd_crawl(args) -> int:
    start = args.url.rstrip("/") + "/"
    root_host = norm_host(urllib.parse.urlsplit(start).netloc)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(urllib.parse.urljoin(start, "/robots.txt"))
    try:
        rp.read()
    except Exception:
        rp = None

    queue: deque[tuple[str, int, str]] = deque()
    seen: set[str] = set()
    sources: dict[str, set[str]] = defaultdict(set)

    def enqueue(u: str, depth: int, src: str):
        n = normalize(u, start)
        if not n or not same_site(n, root_host):
            return
        if SKIP_EXT.search(urllib.parse.urlsplit(n).path):
            return
        key = dedupe_key(n)
        sources[key].add(src)
        if key not in seen:
            seen.add(key)
            queue.append((n, depth, src))

    enqueue(start, 0, "(start)")
    sitemap_urls: list[str] = []
    if not args.no_sitemap:
        print("[*] reading robots.txt / sitemaps ...", file=sys.stderr)
        sitemap_urls = read_sitemaps(start, args.timeout)
        for u in sitemap_urls:
            enqueue(u, 1, "(sitemap)")
        print(f"[*] sitemap URLs: {len(sitemap_urls)}", file=sys.stderr)

    rows: list[dict] = []
    images: dict[str, dict] = {}
    template_hits: Counter = Counter()
    generators: Counter = Counter()
    crawled = 0
    t0 = time.time()

    interrupted = False
    while queue and crawled < args.max_pages:
        try:
            url, depth, src = queue.popleft()
            crawled = _crawl_one(url, depth, args, rp, rows, images, template_hits,
                                 generators, enqueue, root_host, crawled, queue, t0)
        except KeyboardInterrupt:
            interrupted = True
            print("\n[!] interrupted: writing partial results ...", file=sys.stderr)
            break
    _write_crawl(out, rows, images, sources, sitemap_urls, template_hits, generators,
                 crawled, queue, args, t0, start, interrupted)
    return 0


def _crawl_one(url, depth, args, rp, rows, images, template_hits, generators,
               enqueue, root_host, crawled, queue, t0):
    if True:
        if rp and not args.ignore_robots and not rp.can_fetch(UA, url):
            rows.append({"url": url, "path": path_of(url), "status": "robots-blocked",
                         "depth": depth})
            return crawled
        r = fetch(url, args.timeout)
        crawled += 1
        if crawled % 25 == 0:
            print(f"[*] {crawled} pages, queue {len(queue)}, {time.time()-t0:.0f}s", file=sys.stderr)

        ctype = r["headers"].get("Content-Type", r["headers"].get("content-type", ""))
        final = r["final_url"]
        row = {
            "url": url,
            "path": path_of(url),
            "status": r["status"],
            "final_url": final if final != url else "",
            "redirect_chain": " > ".join(f"{s}:{path_of(u)}" for s, u in r["chain"]),
            "content_type": ctype.split(";")[0],
            "depth": depth,
            "found_on": "",
            "title": "", "h1": "", "canonical": "", "robots": "",
            "option": "", "view": "", "category": "",
            "images": 0, "error": r.get("error", ""),
        }
        if r["chain"]:
            row["category"] = "redirect" if same_site(final, root_host) else "redirect-offsite"
        if r["chain"] and not same_site(final, root_host):
            pass
        elif r["status"] == 200 and "html" in ctype:
            pp = PageParser()
            try:
                pp.feed(r["body"])
            except Exception:
                pass
            row["title"] = " ".join(pp.title.split())[:200]
            row["h1"] = " ".join(pp.h1.split())[:200]
            row["canonical"] = pp.canonical
            row["robots"] = pp.robots
            if pp.generator:
                generators[pp.generator] += 1
            option, view = joomla_route(final, pp.body_class)
            row["option"], row["view"] = option, view
            if not r["chain"]:
                row["category"] = classify(final, option, view, row["title"])
            for asset in pp.assets + pp.images:
                for rx, label in TEMPLATE_HINTS:
                    m = rx.search(asset)
                    if m:
                        template_hits[label.format(*m.groups()) if m.groups() else label] += 1
            page_imgs = set()
            for im in pp.images:
                n = normalize(im, final)
                if not n:
                    continue
                page_imgs.add(n)
                rec = images.setdefault(n, {"image": n, "path": path_of(n),
                                            "same_site": same_site(n, root_host),
                                            "pages": 0, "first_page": path_of(url),
                                            "status": ""})
                rec["pages"] += 1
            row["images"] = len(page_imgs)
            if depth < args.max_depth:
                for link in pp.links:
                    enqueue(link, depth + 1, path_of(url))
        elif r["status"] and r["status"] >= 400:
            row["category"] = "error"
        rows.append(row)
        if args.delay:
            time.sleep(args.delay)
    return crawled


def _write_crawl(out, rows, images, sources, sitemap_urls, template_hits, generators,
                 crawled, queue, args, t0, start, interrupted):
    for row in rows:
        srcs = sorted(s for s in sources.get(dedupe_key(row["url"]), ()) if s)
        row["found_on"] = " | ".join(srcs[:3]) + (f" (+{len(srcs)-3})" if len(srcs) > 3 else "")
        row["inlinks"] = len(srcs)

    # Optional: verify images (HEAD) so missing assets show up before migration.
    if args.check_images:
        print(f"[*] checking {len(images)} images ...", file=sys.stderr)
        for i, rec in enumerate(images.values()):
            if not rec["same_site"]:
                rec["status"] = "offsite"
                continue
            r = fetch(rec["image"], args.timeout, method="HEAD")
            if r["status"] in (405, 501):
                r = fetch(rec["image"], args.timeout)
            rec["status"] = r["status"]
            if args.delay:
                time.sleep(args.delay / 2)

    fields = ["url", "path", "status", "category", "option", "view", "title", "h1",
              "canonical", "robots", "final_url", "redirect_chain", "content_type",
              "depth", "inlinks", "found_on", "images", "error"]
    with open(out / "urls.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in sorted(rows, key=lambda r: (str(r.get("category", "")), r["path"])):
            w.writerow(row)
    with open(out / "images.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["image", "path", "same_site", "status", "pages", "first_page"])
        w.writeheader()
        for rec in sorted(images.values(), key=lambda r: r["path"]):
            w.writerow(rec)
    meta = {
        "tool": f"cms-migration-url-guard {VERSION}",
        "site": start,
        "crawled_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pages_requested": crawled,
        "queue_left": len(queue),
        "max_pages": args.max_pages,
        "sitemap_urls": len(sitemap_urls),
        "sitemap_sample": sitemap_urls[:5],
        "generators": dict(generators),
        "template_fingerprint": dict(template_hits.most_common(30)),
        "duration_s": round(time.time() - t0, 1),
        "interrupted": interrupted,
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    print(f"[+] done: {crawled} URLs, {len(images)} images -> {out}/", file=sys.stderr)
    if queue and not interrupted:
        print(f"[!] stopped at --max-pages {args.max_pages}; {len(queue)} URLs still queued. "
              f"Re-run with a higher --max-pages for full coverage.", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------

def load_rows(folder: Path) -> tuple[list[dict], list[dict], dict]:
    with open(folder / "urls.csv", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    imgs = []
    if (folder / "images.csv").exists():
        with open(folder / "images.csv", encoding="utf-8") as f:
            imgs = list(csv.DictReader(f))
    meta = json.loads((folder / "meta.json").read_text()) if (folder / "meta.json").exists() else {}
    return rows, imgs, meta


def url_shape(path: str) -> str:
    """Collapse a path into a pattern: numbers -> {id}, slugs -> {slug}."""
    p = urllib.parse.urlsplit(path)
    segs = []
    for s in p.path.strip("/").split("/"):
        if not s:
            continue
        if re.fullmatch(r"\d+", s):
            segs.append("{id}")
        elif re.fullmatch(r"\d+-[\w-]+(\.html?)?", s):
            segs.append("{id}-{slug}" + (".html" if s.endswith(".html") else ""))
        elif re.search(r"\d", s) or len(s) > 30 or s.count("-") >= 3:
            segs.append("{slug}" + (".html" if s.endswith(".html") else ""))
        else:
            segs.append(s)
    shape = "/" + "/".join(segs)
    if p.query:
        keys = sorted(k for k, _ in urllib.parse.parse_qsl(p.query, keep_blank_values=True))
        q = urllib.parse.parse_qs(p.query)
        parts = []
        for k in keys:
            if k in ("option", "view", "task", "layout"):
                parts.append(f"{k}={q.get(k, [''])[0]}")
            else:
                parts.append(f"{k}={{…}}")
        shape += "?" + "&".join(dict.fromkeys(parts))
    return shape


def md_table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for r in rows:
        out.append("| " + " | ".join(str(c).replace("|", "\\|") for c in r) + " |")
    return "\n".join(out)


def cmd_report(args) -> int:
    folder = Path(args.folder)
    rows, imgs, meta = load_rows(folder)
    html_ok = [r for r in rows if r["status"] == "200" and "html" in r.get("content_type", "")
               and not r.get("redirect_chain")]
    by_cat = Counter(r["category"] or "(unclassified)" for r in html_ok)
    redirects = [r for r in rows if r.get("redirect_chain")]
    errors = [r for r in rows if r["status"] not in ("200",) and not r.get("redirect_chain")]
    out_scope = [r for r in rows if (r.get("category") or "").startswith("out-of-scope")]
    no_canon = [r for r in html_ok if not r.get("canonical")]
    noindex = [r for r in html_ok if "noindex" in (r.get("robots") or "").lower()]
    nonsef = [r for r in html_ok if "option=" in r["path"]]
    idxprefix = [r for r in html_ok if r["path"].startswith("/index.php/")]

    shapes: dict[str, Counter] = defaultdict(Counter)
    shape_example: dict[tuple, str] = {}
    for r in html_ok:
        cat = r["category"] or "(unclassified)"
        sh = url_shape(r["path"])
        shapes[cat][sh] += 1
        shape_example.setdefault((cat, sh), r["path"])

    img_prefix = Counter()
    for im in imgs:
        if im.get("same_site") == "True":
            parts = im["path"].lstrip("/").split("/")
            img_prefix["/" + "/".join(parts[:2]) + "/" if len(parts) > 2 else "/" + parts[0]] += 1
    img_broken = [im for im in imgs if im.get("status") and im["status"] not in ("200", "offsite", "")]
    img_offsite = [im for im in imgs if im.get("same_site") == "False"]

    tf = meta.get("template_fingerprint", {})
    site = meta.get("site", "")

    L: list[str] = []
    L.append(f"# Pre-migration URL audit: {urllib.parse.urlsplit(site).netloc or folder.name}")
    L.append("")
    L.append(f"Crawled {meta.get('crawled_at', '?')} with {meta.get('tool', 'cms-migration-url-guard')}. "
             f"{meta.get('pages_requested', len(rows))} URLs requested, "
             f"{meta.get('sitemap_urls', 0)} found in the XML sitemap.")
    if meta.get("queue_left"):
        L.append(f"\n> Crawl stopped before finishing with {meta['queue_left']} URLs still queued. "
                 f"Numbers below are a lower bound.")
    L.append("")
    L.append("## Summary")
    L.append("")
    L.append(md_table(["Metric", "Count"], [
        ["Live HTML pages (200)", len(html_ok)],
        ["URLs that already redirect", len(redirects)],
        ["Errors / non-200", len(errors)],
        ["Pages for out-of-scope features (need 301 or 410)", len(out_scope)],
        ["Pages with no canonical tag", len(no_canon)],
        ["Non-SEF URLs (index.php?option=…)", len(nonsef)],
        ["Duplicate /index.php/… URLs", len(idxprefix)],
        ["Images referenced", len(imgs)],
        ["Broken images", len(img_broken)],
    ]))
    L.append("")
    L.append("## What the site is made of")
    L.append("")
    L.append(md_table(["Page type", "Pages"], [[c, n] for c, n in by_cat.most_common()]))
    L.append("")
    if meta.get("generators"):
        L.append("Generator tag: " + ", ".join(f"`{g}`" for g in meta["generators"]))
        L.append("")
    if tf:
        L.append("Template and script fingerprint (asset references seen across pages):")
        L.append("")
        L.append(md_table(["Signal", "References"], [[k, v] for k, v in list(tf.items())[:15]]))
        L.append("")

    L.append("## URL patterns to preserve")
    L.append("")
    L.append("Each pattern below must resolve on the new site, either unchanged or through a 301.")
    L.append("")
    pr = []
    for cat in sorted(shapes, key=lambda c: -sum(shapes[c].values())):
        for sh, n in shapes[cat].most_common(6):
            pr.append([cat, f"`{sh}`", n, f"`{shape_example[(cat, sh)][:90]}`"])
    L.append(md_table(["Type", "Pattern", "Pages", "Example"], pr))
    L.append("")

    L.append("## Migration risks found")
    L.append("")
    risks = []
    if any("T3" in k for k in tf):
        risks.append("**T3 framework in use.** T3 does not run on Joomla 6, so the template has to be "
                     "rebuilt (Cassiopeia child or a lightweight custom template). Any layout that relies on "
                     "T3 megamenu or T3 module positions needs mapping to new positions.")
    osp = sum(n for c, n in by_cat.items() if c.startswith("osproperty"))
    if osp:
        risks.append(f"**{osp} OS Property pages.** Listing URLs come from OS Property's own router. "
                     "If the router or menu item aliases change during the OS Property upgrade, "
                     "every listing URL changes. The URL inventory in `urls.csv` is the baseline to replay "
                     "against staging.")
    if out_scope:
        oc = Counter(r["category"].split(":", 1)[1] for r in out_scope)
        risks.append("**Out-of-scope features still have public URLs:** "
                     + ", ".join(f"{k} ({v})" for k, v in oc.most_common())
                     + ". Each needs a decision: 301 to the closest page, or 410 Gone.")
    if nonsef:
        risks.append(f"**{len(nonsef)} non-SEF URL(s)** (`index.php?option=…`) are linked or indexed. "
                     "Joomla 6 routing will not generate these, so they need explicit redirects.")
    if idxprefix:
        risks.append(f"**{len(idxprefix)} page(s) also answer under an `/index.php/…` prefix**, which "
                     "duplicates the clean URLs. On the new site, `/index.php/<path>` should 301 to `/<path>`.")
    if any("MooTools" in k for k in tf) or any("jQuery 1" in k for k in tf):
        risks.append("**Legacy JavaScript (MooTools / jQuery 1.x)** detected. Custom OS Property "
                     "layout code that depends on it has to be rewritten for Joomla 6.")
    if any("Slider" in k for k in tf) or any("Popup" in k for k in tf):
        risks.append("**Slider / popup scripts** still load on the live site. If these features are "
                     "being dropped, the pages and module positions that use them need checking so "
                     "nothing is left empty or broken.")
    if img_broken:
        risks.append(f"**{len(img_broken)} image(s) already return errors** on the live site. "
                     "Worth fixing or excluding before migration so they are not counted as migration faults.")
    if img_offsite:
        risks.append(f"**{len(img_offsite)} image(s) load from other domains** and will not move with the site.")
    if no_canon:
        risks.append(f"**{len(no_canon)} page(s) have no canonical tag.** The new template should emit "
                     "canonicals so duplicate listing URLs (filters, pagination) don't compete in search.")
    if noindex:
        risks.append(f"**{len(noindex)} page(s) are set to noindex.** Confirm that is intended before "
                     "the new template copies (or drops) it.")
    if not risks:
        risks.append("No blocking risks detected from the public crawl.")
    for r in risks:
        L.append(f"- {r}")
    L.append("")

    if img_prefix:
        L.append("## Image locations")
        L.append("")
        L.append("Paths that must exist unchanged on the new site:")
        L.append("")
        L.append(md_table(["Folder", "Images"], [[f"`{k}`", v] for k, v in img_prefix.most_common(12)]))
        L.append("")

    if redirects:
        L.append("## Existing redirects")
        L.append("")
        L.append("These already redirect today and must keep working:")
        L.append("")
        L.append(md_table(["From", "Chain", "To"],
                          [[f"`{r['path'][:70]}`", r["redirect_chain"][:80],
                            f"`{path_of(r['final_url'])[:70]}`"] for r in redirects[:25]]))
        if len(redirects) > 25:
            L.append(f"\n…and {len(redirects) - 25} more in `urls.csv`.")
        L.append("")

    if errors:
        L.append("## Errors on the live site today")
        L.append("")
        L.append(md_table(["URL", "Status", "Linked from"],
                          [[f"`{r['path'][:80]}`", r["status"], r.get("found_on", "")[:60]]
                           for r in errors[:25]]))
        L.append("")

    L.append("## Files")
    L.append("")
    L.append("- `urls.csv`: every URL with status, page type, Joomla component/view, title, canonical, inlinks")
    L.append("- `images.csv`: every referenced image with path and status")
    L.append("- `meta.json`: crawl settings and template fingerprint")
    L.append("")
    L.append("After the rebuild, `urlguard.py diff` replays every URL in `urls.csv` against staging "
             "and produces a pass/fail sheet plus a draft 301 list.")

    (folder / "report.md").write_text("\n".join(L), encoding="utf-8")
    print(f"[+] wrote {folder / 'report.md'}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# diff
# --------------------------------------------------------------------------

def apache_re(text: str) -> str:
    return re.sub(r"([.^$*+?()\[\]{}|\\])", r"\\\1", text)


def similar(a: str, b: str) -> float:
    ta = set(re.findall(r"\w+", a.lower()))
    tb = set(re.findall(r"\w+", b.lower()))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def cmd_diff(args) -> int:
    folder = Path(args.folder)
    rows, imgs, _meta = load_rows(folder)
    new_base = args.new_url.rstrip("/")
    new_host = norm_host(urllib.parse.urlsplit(new_base).netloc)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    targets = [r for r in rows if r.get("redirect_chain")
               or (r["status"] == "200" and "html" in r.get("content_type", ""))]

    # Map: title -> new path, built as we go, for redirect suggestions.
    results = []
    new_titles: dict[str, str] = {}
    new_paths: list[str] = []
    if args.new_inventory:
        nrows, _, _ = load_rows(Path(args.new_inventory))
        for nr in nrows:
            if nr["status"] == "200" and not nr.get("redirect_chain"):
                new_paths.append(nr["path"])
                if nr.get("title"):
                    new_titles[" ".join(nr["title"].split())] = nr["path"]
    for i, r in enumerate(targets, 1):
        url = new_base + r["path"]
        res = fetch(url, args.timeout)
        title = ""
        if res["status"] == 200:
            pp = PageParser()
            try:
                pp.feed(res["body"])
            except Exception:
                pass
            title = " ".join(pp.title.split())
            new_titles.setdefault(title, path_of(res["final_url"]))
            new_paths.append(path_of(res["final_url"]))
        cat = r.get("category", "")
        old_title = r.get("title", "")
        final_path = path_of(res["final_url"])
        if cat.startswith("out-of-scope"):
            verdict = "PASS" if res["status"] in (301, 410, 200) or res["chain"] else "CHECK"
            if res["status"] == 404 and not res["chain"]:
                verdict = "NEEDS 301/410"
        elif r.get("redirect_chain"):
            if res["chain"] and res["status"] == 200:
                kinds = {s for s, _ in res["chain"]}
                verdict = "PASS" if kinds <= {301, 308} else "TEMP REDIRECT (use 301)"
            else:
                verdict = "BROKEN REDIRECT"
        elif res["status"] == 200 and not res["chain"]:
            sim = similar(old_title, title)
            verdict = "PASS" if sim >= args.title_threshold or not old_title else "CONTENT CHANGED"
        elif res["status"] == 200 and res["chain"]:
            kinds = {s for s, _ in res["chain"]}
            verdict = "PASS (301)" if kinds <= {301, 308} else "TEMP REDIRECT (use 301)"
        else:
            verdict = "MISSING"
        results.append({
            "old_path": r["path"], "category": cat, "old_title": old_title,
            "new_status": res["status"], "new_final": final_path if res["chain"] else "",
            "new_title": title, "verdict": verdict, "error": res.get("error", ""),
        })
        if i % 25 == 0:
            print(f"[*] {i}/{len(targets)}", file=sys.stderr)
        if args.delay:
            time.sleep(args.delay)

    # Suggest 301 targets for MISSING rows by matching old titles to new titles.
    def last_slug(path: str) -> str:
        seg = urllib.parse.urlsplit(path).path.rstrip("/").rsplit("/", 1)[-1]
        return re.sub(r"\.html?$", "", seg.lower())

    slug_index: dict[str, str] = {}
    for np_ in new_paths:
        sl = last_slug(np_)
        if sl and len(sl) > 3:
            slug_index.setdefault(sl, np_)

    redirects = []
    for row in results:
        if row["verdict"] in ("MISSING", "NEEDS 301/410"):
            # 1) same final slug at a new location (e.g. /properties/12-x -> /listings/12-x)
            best = slug_index.get(last_slug(row["old_path"]), "")
            if not best:
                score = 0.0
                for t, p in new_titles.items():
                    sc = similar(row["old_title"], t)
                    if sc > score:
                        best, score = p, sc
                if score < 0.6:
                    best = ""
            row["suggested_target"] = best
            redirects.append(row)

    img_results = []
    if args.check_images and imgs:
        for im in imgs:
            if im.get("same_site") != "True":
                continue
            r = fetch(new_base + im["path"], args.timeout, method="HEAD")
            if r["status"] in (405, 501):
                r = fetch(new_base + im["path"], args.timeout)
            was_broken = im.get("status") not in ("", "200", None)
            verdict = "PASS" if r["status"] == 200 else ("ALREADY BROKEN ON OLD SITE" if was_broken else "MISSING")
            img_results.append({"path": im["path"], "new_status": r["status"], "verdict": verdict})

    fields = ["verdict", "old_path", "category", "new_status", "new_final",
              "old_title", "new_title", "suggested_target", "error"]
    with open(out / "diff.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in sorted(results, key=lambda r: (r["verdict"].startswith("PASS"), r["old_path"])):
            w.writerow(row)
    if img_results:
        with open(out / "images-diff.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["verdict", "path", "new_status"])
            w.writeheader()
            for row in sorted(img_results, key=lambda r: r["verdict"]):
                w.writerow(row)

    # Draft .htaccess redirects (Apache / cPanel).
    lines = ["# Draft 301 redirects generated by cms-migration-url-guard",
             "# Review every line before use. Rows without a target need a manual decision.",
             "<IfModule mod_rewrite.c>", "RewriteEngine On"]
    for row in redirects:
        old = row["old_path"]
        tgt = row.get("suggested_target") or ""
        p = urllib.parse.urlsplit(old)
        if p.query:
            lines.append(f"RewriteCond %{{QUERY_STRING}} ^{apache_re(p.query)}$")
            rule_path = apache_re(p.path.lstrip("/"))
            if tgt:
                lines.append(f"RewriteRule ^{rule_path}$ {tgt}? [R=301,L]")
            else:
                lines.append(f"# TODO target: RewriteRule ^{rule_path}$ /NEW-PATH? [R=301,L]")
        else:
            if tgt:
                lines.append(f"RewriteRule ^{apache_re(old.lstrip('/'))}$ {tgt} [R=301,L]")
            else:
                lines.append(f"# TODO target for {old}  (or: RewriteRule ... - [G,L] for 410)")
    lines.append("</IfModule>")
    (out / "redirects.htaccess").write_text("\n".join(lines) + "\n", encoding="utf-8")

    v = Counter(r["verdict"] for r in results)
    iv = Counter(r["verdict"] for r in img_results)
    summary = ["# Migration diff: old URLs replayed on " + new_host, "",
               md_table(["Verdict", "URLs"], [[k, n] for k, n in v.most_common()]), ""]
    if img_results:
        summary += ["Images: " + ", ".join(f"{k} {n}" for k, n in iv.most_common()), ""]
    bad = [r for r in results if not r["verdict"].startswith("PASS")]
    if bad:
        summary += ["## Needs attention", "",
                    md_table(["Verdict", "Old path", "New status", "Suggested target"],
                             [[r["verdict"], f"`{r['old_path'][:70]}`", r["new_status"],
                               f"`{r.get('suggested_target', '')}`" if r.get("suggested_target") else ""]
                              for r in bad[:60]]), ""]
    total = len(results)
    passed = sum(n for k, n in v.items() if k.startswith("PASS"))
    summary.append(f"**{passed}/{total} URLs pass.** Go-live gate: every row PASS or an agreed 410.")
    (out / "diff.md").write_text("\n".join(summary), encoding="utf-8")
    print(f"[+] {passed}/{total} pass -> {out}/diff.md, diff.csv, redirects.htaccess", file=sys.stderr)
    return 0 if passed == total else 2


# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="urlguard", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=VERSION)
    sub = ap.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl", help="crawl a live site and build the URL inventory")
    c.add_argument("url")
    c.add_argument("--out", default="audit")
    c.add_argument("--max-pages", type=int, default=3000)
    c.add_argument("--max-depth", type=int, default=12)
    c.add_argument("--delay", type=float, default=0.3, help="seconds between requests (be polite)")
    c.add_argument("--timeout", type=float, default=20)
    c.add_argument("--no-sitemap", action="store_true")
    c.add_argument("--check-images", action="store_true", help="HEAD every same-site image")
    c.add_argument("--ignore-robots", action="store_true")
    c.set_defaults(func=cmd_crawl)

    r = sub.add_parser("report", help="write report.md from a crawl folder")
    r.add_argument("folder")
    r.set_defaults(func=cmd_report)

    d = sub.add_parser("diff", help="replay old URLs against the new site")
    d.add_argument("folder", help="crawl folder of the OLD site")
    d.add_argument("new_url", help="base URL of the new/staging site")
    d.add_argument("--out", default="diff")
    d.add_argument("--delay", type=float, default=0.2)
    d.add_argument("--timeout", type=float, default=20)
    d.add_argument("--title-threshold", type=float, default=0.5)
    d.add_argument("--check-images", action="store_true")
    d.add_argument("--new-inventory", help="crawl folder of the NEW site, used to suggest 301 targets")
    d.set_defaults(func=cmd_diff)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
