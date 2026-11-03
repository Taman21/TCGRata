"""TCG price crawler. Discovers Chilean TCG stores, indexes their catalogs, tracks price history.

Usage:
  python crawler.py               discover new stores + crawl all + build site/
  python crawler.py --no-discover crawl only known stores (data.json + stores.txt)
  python crawler.py --report      rebuild site/ from data.json, no network
  python crawler.py --health      list stores without a clean crawl in the last 24 h (exit 1 if >10% are stale)
  python crawler.py --serve       open site/ at http://localhost:8000 (the page fetches data/, so file:// won't work)
  python crawler.py --buscar "delta reign,dominio delta"   fast targeted search (comma = aliases) -> site/delta-reign.html

Store discovery uses the Brave Search API when BRAVE_API_KEY is set (Windows: setx BRAVE_API_KEY "tu-key",
then open a new terminal). Without it, falls back to scraping DuckDuckGo/Bing, which gets captcha-blocked quickly.
"""
import base64, html, json, os, re, sys, unicodedata, time, urllib.error, urllib.parse, urllib.request, zlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import classify

DIR = Path(__file__).parent
DATA, SEEDS, SITE = DIR / "data.json", DIR / "stores.txt", DIR / "site"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
QUERIES = [
    "tienda pokemon tcg chile", "comprar cartas pokemon chile", "booster box pokemon chile",
    "elite trainer box pokemon chile", "cartas pokemon singles chile", "tienda tcg chile",
    "tienda cartas coleccionables chile", "one piece tcg chile tienda", "magic the gathering tienda chile",
    "preventa pokemon tcg chile", "preventa elite trainer box chile",
]
BLOCK = re.compile(r"mercadolibre|falabella|paris\.cl|ripley|lider\.cl|jumbo\.cl|amazon|ebay|aliexpress|temu|"
                   r"facebook|instagram|youtube|tiktok|reddit|wikipedia|pokemon\.com|duckduckgo|bing\.com|google|"
                   r"preciostcg")  # price aggregators: they'd duplicate other stores' prices
TCG = re.compile(r"pok[eé]mon|\btcg\b|booster|elite trainer|\bcartas?\b|magic|yu-?gi-?oh|one piece|lorcana|\b\d{1,3}/\d{2,3}\b", re.I)
BRAVE_KEY = os.environ.get("BRAVE_API_KEY", "").strip()
DELAY = 0.4  # seconds between requests to the same store
MAX_PAGES = 400
MAX_SITEMAP_URLS = 6000  # ponytail: caps slow page-by-page stores; raise if a big store gets truncated


def fetch(url, timeout=25, tries=4, headers=None):
    """GET with backoff on 429/5xx/timeouts. Raises after the last try so callers never mistake an error for 'no more pages'."""
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={**UA, **(headers or {})}), timeout=timeout) as r:
                return r.read().decode(r.headers.get_content_charset() or "utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500 or i == tries - 1:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if i == tries - 1:
                raise
        time.sleep(5 * (i + 1))


def fetch_json(url):
    try:
        return json.loads(fetch(url))
    except Exception:
        return None


# ---------- discovery ----------
def brave_urls(q):
    """Brave Search API (key in env BRAVE_API_KEY). Two pages of 20 results, Chile-targeted."""
    urls = []
    for offset in range(2):
        d = json.loads(fetch("https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(
            {"q": q, "country": "CL", "search_lang": "es", "count": 20, "offset": offset}),
            headers={"Accept": "application/json", "X-Subscription-Token": BRAVE_KEY}))
        urls += [r["url"] for r in d.get("web", {}).get("results", [])]
        time.sleep(1.1)  # free plan allows 1 request/second
    return urls


def search_urls(q):
    """Brave API when a key is set; otherwise scrape DuckDuckGo + Bing HTML (both captcha-block after bursts)."""
    global BRAVE_KEY
    if BRAVE_KEY:
        try:
            return brave_urls(q)
        except urllib.error.HTTPError as e:
            print(f"  Brave API error {e.code}: {e.read()[:200]!r} -> usando scraping DDG/Bing")
            BRAVE_KEY = ""  # don't retry a bad key for every query
    urls = []
    try:
        page = fetch("https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": q, "kl": "cl-es"}))
        urls += [urllib.parse.unquote(u) for u in re.findall(r'uddg=([^&"]+)', page)]
    except Exception:
        pass
    try:
        page = fetch("https://www.bing.com/search?" + urllib.parse.urlencode({"q": q, "cc": "CL", "count": "50"}))
        for u in re.findall(r'[?&;]u=a1([A-Za-z0-9_-]+)', page):
            urls.append(base64.urlsafe_b64decode(u + "=" * (-len(u) % 4)).decode("utf-8", "replace"))
    except Exception:
        pass
    return urls


def discover(queries=QUERIES):
    found = set()
    for q in queries:
        hosts = {urllib.parse.urlparse(u).netloc.lower() for u in search_urls(q)}
        found |= {h for h in hosts if h.endswith(".cl") and not BLOCK.search(h)}
        if not BRAVE_KEY:
            time.sleep(1.5)  # scraped search rate-limits bursts
    print(f"  {len(found)} dominios encontrados" + ("" if BRAVE_KEY else " (sin BRAVE_API_KEY: scraping DDG/Bing, resultados limitados)"))
    return found


# ---------- platform adapters: each yields dicts {id,title,url,price,available,image} ----------
def cat_text(*parts):
    """Store-side category/tags/vendor as one string: the strongest hint of a product's game (classify.py reads it)."""
    return " | ".join(dict.fromkeys(html.unescape(str(x)).strip() for x in parts if x and str(x).strip()))


def paged(fetch_page, workers=4, key=None, max_pages=MAX_PAGES):
    """Yield items of fetch_page(n) for n=1.., `workers` pages at a time; stop at the first empty page.
    key: for platforms that repeat the last page instead of returning nothing, a page with no unseen key counts as empty.
    If a parallel batch fails (rate limit, 429), that page is retried alone and the rest go one at a time, slower."""
    seen, n, step = set(), 1, workers
    with ThreadPoolExecutor(workers) as ex:
        while n < max_pages:
            try:
                batch = list(ex.map(fetch_page, range(n, n + step))) if step > 1 else [fetch_page(n)]
            except Exception:
                if step == 1:
                    raise
                step = 1
                continue
            for items in batch:
                if key:
                    fresh = [i for i in items if key(i) not in seen]
                    seen.update(key(i) for i in fresh)
                    items = fresh
                if not items:
                    return
                yield from items
            n += step
            time.sleep(DELAY * (1 if step > 1 else 3))


def shopify(host):
    def page(n):
        out = []
        try:
            products = json.loads(fetch(f"https://{host}/products.json?limit=250&page={n}")).get("products", [])
        except urllib.error.HTTPError as e:
            # ponytail: Shopify answers 400 past page 100 (25,000 products): a store that big is read only up to there; since_id is the upgrade path
            if e.code == 400 and n > 1:
                return []
            raise
        for p in products:
            v = p.get("variants") or [{}]
            tags = p.get("tags") or []
            out.append({"id": p["id"], "title": p["title"], "url": f"https://{host}/products/{p['handle']}",
                        "price": float(v[0].get("price") or 0), "available": any(x.get("available") for x in v),
                        "image": (p.get("images") or [{}])[0].get("src"),
                        "cat": cat_text(p.get("product_type"), p.get("vendor"), *(tags.split(",") if isinstance(tags, str) else tags))})
        return out
    yield from paged(page)


def woocommerce_page(host, query):
    out = []
    # Stores with "hide out of stock items" drop sold-out products from the API unless every status is requested
    statuses = "&stock_status[]=instock&stock_status[]=outofstock&stock_status[]=onbackorder"
    for p in json.loads(fetch(f"https://{host}/wp-json/wc/store/v1/products?{query}{statuses}")):
        pr = p.get("prices") or {}
        price = int(pr.get("price") or 0) / 10 ** int(pr.get("currency_minor_unit") or 0)
        out.append({"id": p["id"], "title": p["name"], "url": p["permalink"], "price": price,
                    "available": bool(p.get("is_in_stock")), "image": (p.get("images") or [{}])[0].get("src"),
                    "cat": cat_text(*(c.get("name") for c in (p.get("categories") or []) + (p.get("tags") or []) + (p.get("brands") or [])))})
    return out


def woocommerce(host):
    yield from paged(lambda n: woocommerce_page(host, f"per_page=100&page={n}"))


def jsonld_products(page_html, page_url=None):
    for block in re.findall(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', page_html, re.S):
        try:
            d = json.loads(block, strict=False)
        except ValueError:
            continue
        for p in d if isinstance(d, list) else d.get("@graph", [d]):
            if isinstance(p, dict) and p.get("@type") == "Product":
                o = p.get("offers") or {}
                o = o[0] if isinstance(o, list) else o
                url = o.get("url") or (p.get("mainEntityOfPage") or {}).get("@id") or page_url
                img = p.get("image")
                img = img[0] if isinstance(img, list) and img else img
                yield {"id": url, "title": p.get("name", ""), "url": url, "price": float(o.get("price") or o.get("lowPrice") or 0),
                       "available": "InStock" in str(o.get("availability")),
                       "image": img.get("url") if isinstance(img, dict) else img,
                       "cat": cat_text(p.get("category"), (p.get("brand") or {}).get("name") if isinstance(p.get("brand"), dict) else p.get("brand"))}


def tiendanube(host):
    seen = set()
    for page in range(1, MAX_PAGES):
        items = [p for p in jsonld_products(fetch(f"https://{host}/productos/page/{page}/")) if p["id"] not in seen]
        if not items:  # past last page Tiendanube repeats or returns nothing
            return
        for p in items:
            seen.add(p["id"])
            yield p
        time.sleep(DELAY)


def odoo_cards(host, path):
    """Odoo eCommerce listing cards (schema.org microdata). Past the last page Odoo repeats it, so callers dedupe."""
    for card in fetch(f"https://{host}{path}").split("oe_product_cart")[1:]:
        url, name = re.search(r'itemprop="url" href="([^"?]+)', card), re.search(r'itemprop="name" content="([^"]*)"', card)
        if url and name:
            price, img = re.search(r'itemprop="price"[^>]*>([\d.]+)<', card), re.search(r'<img[^>]+src="([^"]+)"', card)
            yield {"id": url[1], "title": name[1], "url": f"https://{host}{url[1]}", "price": float(price[1]) if price else 0,
                   # ponytail: badge-less cards were AGOTADO on geekz.cl; other Odoo themes may need a real stock check
                   "available": 'class="corner"' in card, "image": img and urllib.parse.urljoin(f"https://{host}/", img[1])}


def odoo(host, search=""):
    # 15 per page and no way to raise it, hence the parallel pages; past the last page Odoo repeats it, so `key` ends the walk
    yield from paged(lambda n: list(odoo_cards(host, f"/shop/page/{n}?search={search}")), key=lambda p: p["id"], max_pages=MAX_PAGES * 2)


def sitemap_entries(host):
    """{url: lastmod or ""} from the sitemap tree."""
    todo, urls = [f"https://{host}/sitemap.xml"], {}
    while todo and len(urls) < MAX_SITEMAP_URLS:
        try:
            entries = re.findall(r"<loc>\s*(.*?)\s*</loc>(?:\s*<lastmod>\s*(.*?)\s*</lastmod>)?", fetch(todo.pop()))
        except Exception:
            continue
        todo += [l for l, _ in entries if l.endswith(".xml")]
        urls.update((html.unescape(l), m) for l, m in entries if not l.endswith(".xml"))
    return dict(list(urls.items())[:MAX_SITEMAP_URLS])


def sitemap_urls(host):
    return list(sitemap_entries(host))


def page_products(url):
    try:
        return list(jsonld_products(fetch(url), url))
    except Exception:
        return []


ROTATE = 4  # sitemaps without lastmod: the non-Pokemon URLs already known are refreshed 1/ROTATE per crawl (each one every ~24 h at a 6 h cron)


def sitemap(host, seen, hot=None, rot=0):
    """Fallback for any platform (Jumpseller, custom...): visit sitemap URLs, keep pages with Product JSON-LD.
    `seen` {url: lastmod} is updated in place; URLs whose lastmod is unchanged since the last crawl are skipped.
    Without a lastmod nothing can be skipped by date, so: new URLs and Pokemon ones (`hot` {url: bool} from earlier crawls) are
    always visited and the rest rotate by `rot`. hot=None visits everything.
    ponytail: trusts lastmod; a store that doesn't bump it on price/stock edits stays stale, add a periodic full pass if seen."""
    entries = sitemap_entries(host)
    for u in [u for u, m in seen.items() if not m]:  # older runs stored "" for every lastmod-less URL
        del seen[u]

    def due(u, m):
        if m:
            return seen.get(u) != m
        return hot is None or u not in hot or hot[u] or zlib.crc32(u.encode()) % ROTATE == rot % ROTATE

    todo = [u for u, m in entries.items() if due(u, m)]
    if len(todo) < len(entries):
        print(f"    {host}: {len(todo)} de {len(entries)} URLs hoy")

    def visit(u):
        try:
            return u, list(jsonld_products(fetch(u), u))
        except Exception:
            return u, None  # failed: leave unrecorded so it's retried next run

    with ThreadPoolExecutor(8) as ex:
        for u, items in ex.map(visit, todo):
            if items is not None:
                if entries[u]:
                    seen[u] = entries[u]
                yield from items


def detect(host):
    """(platform, sample titles) or (None, reason). Samples let us reject non-TCG shops before a full crawl."""
    if (d := fetch_json(f"https://{host}/products.json?limit=50")) and "products" in d:
        return "shopify", [p["title"] for p in d["products"]]
    if isinstance(d := fetch_json(f"https://{host}/wp-json/wc/store/v1/products?per_page=50"), list):
        return "woocommerce", [p["name"] for p in d]
    try:
        home = fetch(f"https://{host}/", tries=1)
        if "tiendanube" in home.lower():
            return "tiendanube", [p["title"] for p in jsonld_products(fetch(f"https://{host}/productos/"))]
        if "var odoo" in home:
            return "odoo", [p["title"] for p in odoo_cards(host, "/shop")]
    except Exception:
        pass
    urls = sitemap_urls(host)
    sample = urls[len(urls) // 4::max(1, len(urls) // 12)][:10]  # skip home/contact pages at the start
    titles = [p["title"] for u in sample for p in page_products(u)]
    return ("sitemap", titles) if titles else (None, [])


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFD", s.lower()).encode("ascii", "ignore").decode()).strip("-")


def search_store(host, platform, q):
    """Use each platform's own search instead of a full crawl. Results are filtered to titles containing every word."""
    enc = urllib.parse.quote(q)
    if platform == "woocommerce":
        items = woocommerce_page(host, f"search={enc}&per_page=100")
    elif platform == "shopify":  # its search API caps at 10 hits; products.json is fast enough to scan whole
        items = list(shopify(host))
    elif platform == "tiendanube":
        items = list(jsonld_products(fetch(f"https://{host}/search/?q={enc}")))
    elif platform == "odoo":
        items = list(odoo(host, enc))
    else:
        urls = {u for u in sitemap_urls(host) if slug(q) in u.lower()}
        for u in list(urls):  # collection/category pages: follow their matching links one hop
            if not page_products(u):
                page = fetch(u)
                urls |= {urllib.parse.urljoin(u, h) for h in re.findall(r'href="([^"#]+)"', page) if slug(q) in h.lower()}
        with ThreadPoolExecutor(4) as ex:
            items = [i for batch in ex.map(page_products, urls) for i in batch]
    words = slug(q).split("-")
    return [i for i in items if all(w in slug(html.unescape(i["title"])) for w in words)]


ADAPTERS = {"shopify": shopify, "woocommerce": woocommerce, "tiendanube": tiendanube, "odoo": odoo, "sitemap": sitemap}


# ---------- crawl ----------
def crawl_store(host, platform, lastmod, hot=None, rot=0):
    t0 = time.time()
    for attempt in range(2):
        items, err = [], None
        try:
            items.extend(sitemap(host, lastmod, hot, rot) if platform == "sitemap" else ADAPTERS[platform](host))
        except Exception as e:  # keep what we got; products not seen this run just keep their old last_seen
            err = f"{type(e).__name__}: {e}"
        if not err or items:  # retry once only when nothing came back at all
            break
    return host, items, err, time.time() - t0


def merge(data, host, items, now):
    for it in items:
        key = f"{host}|{it['id']}"
        title = html.unescape(it["title"]).strip()
        p = data["products"].setdefault(key, {"store": host, "history": [], "first_seen": now})
        p.update(title=title, url=it["url"], image=it.get("image"), price=it["price"],
                 available=it["available"], last_seen=now)
        if it.get("cat"):  # not every platform has one; keep the last known
            p["cat"] = it["cat"]
        last = p["history"][-1] if p["history"] else None
        if not last or last[1] != it["price"] or last[2] != it["available"]:
            p["history"].append([now, it["price"], it["available"]])


def health(data, max_age_h=24):
    """Stores whose last clean crawl is older than max_age_h. Exits 1 when more than 10% (min 5) are stale: that points to an
    outage or a block, not one broken shop. Single stores show in the log, the run annotations and the site footer."""
    now = datetime.now().astimezone()
    age = lambda s: (now - datetime.fromisoformat(s["last_ok"]).astimezone()).total_seconds() / 3600 if s.get("last_ok") else None
    live = {h: s for h, s in data["stores"].items() if not s.get("disabled")}
    stale = {h: s for h, s in sorted(live.items()) if age(s) is None or age(s) > max_age_h}
    lines = [f"{h}: " + (f"sin datos frescos hace {age(s):.0f} h" if age(s) is not None else "sin ningun crawl exitoso")
             + f", {s.get('fails', 0)} fallos seguidos, ultimo error: {s.get('error')}" for h, s in stale.items()]
    print(f"{len(stale)} de {len(live)} tiendas sin datos de menos de {max_age_h} h")
    for l in lines:
        print("  " + l)
    if os.environ.get("GITHUB_ACTIONS"):
        for l in lines:
            print(f"::warning::{l}")
        if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(summary, "a", encoding="utf-8") as f:
                f.write(f"### {len(stale)} de {len(live)} tiendas sin datos frescos (>{max_age_h} h)\n" + "".join(f"- {l}\n" for l in lines))
    sys.exit(1 if len(stale) > max(5, len(live) // 10) else 0)


def main():
    if "--serve" in sys.argv:
        return serve()
    data = json.loads(DATA.read_text("utf-8")) if DATA.exists() else {"stores": {}, "products": {}}
    rejected = data.setdefault("rejected", {})  # host -> date; delete an entry to retry it
    if "--report" in sys.argv:
        return build_report(data)
    if "--health" in sys.argv:
        return health(data)
    now = datetime.now().astimezone().isoformat(timespec="minutes")  # with offset: CI runs in UTC, the page runs in the viewer's zone
    query = sys.argv[sys.argv.index("--buscar") + 1] if "--buscar" in sys.argv else None

    seeds = {l.split("#")[0].strip().lower().removeprefix("https://").removeprefix("http://").strip("/")
             for l in SEEDS.read_text("utf-8").splitlines()} - {""} if SEEDS.exists() else set()
    found = set()
    if "--no-discover" not in sys.argv:
        print("Buscando tiendas...")
        found = discover([f"{q.strip()} {s} chile" for q in query.split(",")
                          for s in ("preventa", "elite trainer box", "booster box")] if query else QUERIES)
    new = (seeds | (found - set(rejected))) - set(data["stores"])
    print(f"{len(new)} candidatas nuevas, detectando plataforma...")
    with ThreadPoolExecutor(8) as ex:
        for host, (platform, titles) in zip(new, ex.map(detect, new)):
            tcg = sum(bool(TCG.search(t)) for t in titles)
            if platform and tcg >= max(2, len(titles) // 10) or host in seeds and platform:
                data["stores"][host] = {"platform": platform, "first_seen": now}
                print(f"  + {host} ({platform}, {tcg}/{len(titles)} TCG en muestra)")
                if host not in seeds:
                    with SEEDS.open("a", encoding="utf-8") as f:
                        f.write(f"{host}  # auto {now[:10]}\n")
            else:
                rejected[host] = now
                why = f"{tcg}/{len(titles)} TCG en muestra" if platform else "plataforma no soportada"
                print(f"  - {host} ({why})")

    for h, s in data["stores"].items():  # commenting a store out of stores.txt retires it: not crawled, not shown
        s["disabled"] = bool(seeds) and h not in seeds
    stores = {h: s for h, s in data["stores"].items() if not s["disabled"]}
    if query:
        return search_all(data, query, now)
    print(f"Crawleando {len(stores)} tiendas...")
    known = Counter(p["store"] for p in data["products"].values())
    t0 = last_save = time.time()
    hot = {}  # per sitemap store: {product id (its URL): looks like Pokemon}, from what earlier crawls found
    for k, p in data["products"].items():
        if stores.get(p["store"], {}).get("platform") == "sitemap":
            hot.setdefault(p["store"], {})[k[len(p["store"]) + 1:]] = classify.is_pokemon_like(p["title"], p.get("cat"))
    with ThreadPoolExecutor(8) as ex:
        for fut in as_completed([ex.submit(crawl_store, h, stores[h]["platform"], stores[h].setdefault("lastmod", {}),
                                           hot.get(h, {}), stores[h].get("rot", 0))
                                 if stores[h]["platform"] == "sitemap" else ex.submit(crawl_store, h, stores[h]["platform"], None) for h in stores]):
            host, items, err, secs = fut.result()
            s = stores[host]
            if s["platform"] == "sitemap":
                s["rot"] = s.get("rot", 0) + 1
            if not err and not items and s["platform"] != "sitemap" and known[host]:  # sitemap stores legitimately return 0 when nothing changed
                err = f"0 productos (antes {known[host]})"  # an empty answer from a store that had stock is a block or an outage
            s.update(last_crawl=now, count=len(items), error=err, secs=round(secs, 1))
            if err:
                s["fails"] = s.get("fails", 0) + 1
            else:
                s.update(last_ok=now, fails=0)  # last_ok is what the page treats as "how fresh are this store's prices"
            merge(data, host, items, now)
            print(f"  {host}: {len(items)} productos en {secs:.0f}s" + (f" (error: {err})" if err else ""))
            if time.time() - last_save > 60:  # dumping ~150 MB per store dominated runtime; a killed run loses <=1 min
                DATA.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
                last_save = time.time()
    DATA.write_text(json.dumps(data, ensure_ascii=False), "utf-8")

    slow = sorted(stores.items(), key=lambda kv: -kv[1].get("secs", 0))[:5]
    print(f"Total {time.time() - t0:.0f}s. Mas lentas: " + ", ".join(f"{h} {s['secs']:.0f}s" for h, s in slow))
    print("Con error: " + (", ".join(h for h, s in stores.items() if s.get("error")) or "ninguna"))
    build_report(data)


def search_all(data, query, now):
    stores = data["stores"]
    print(f"Buscando '{query}' en {len(stores)} tiendas...")

    def one(h):  # query may hold comma-separated aliases, e.g. "delta reign,dominio delta"
        found = {}
        try:
            for q in query.split(","):
                found.update((i["id"], i) for i in search_store(h, stores[h]["platform"], q.strip()))
            return h, list(found.values()), None
        except Exception as e:
            return h, list(found.values()), f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(8) as ex:
        for host, items, err in ex.map(one, list(stores)):
            merge(data, host, items, now)
            print(f"  {host}: {len(items)}" + (f" (error: {err})" if err else ""))
    DATA.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
    build_report(data)
    write_page(slug(query.split(",")[0]), query)


CHUNK = 20000  # groups per file: keeps every file far below Cloudflare Pages' 25 MB limit


def write_site(data):
    """site/data/<game>.<bucket>.<n>.json (grouped products, see classify.build_groups) + index.json. History is dropped:
    the page only needs the previous price."""
    out = SITE / "data"
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.json"):
        f.unlink()
    hosts = sorted(h for h, s in data["stores"].items() if not s.get("disabled"))
    shards = classify.build_groups(data["products"], {h: i for i, h in enumerate(hosts)})
    counts = Counter(p["store"] for p in data["products"].values())  # s["count"] is only what the last run fetched (0 for an unchanged sitemap)
    dump = lambda o: json.dumps(o, ensure_ascii=False, separators=(",", ":"))
    games = {}
    for (game, bucket), groups in shards.items():
        parts = [groups[i:i + CHUNK] for i in range(0, len(groups), CHUNK)]
        for n, part in enumerate(parts):
            (out / f"{game}.{bucket}.{n}.json").write_text(dump(part), "utf-8")
        games.setdefault(game, {})[bucket] = {"groups": len(groups), "parts": len(parts)}
    stores = [{"host": h, **{k: v for k, v in data["stores"][h].items() if k in ("platform", "last_crawl", "last_ok", "fails", "error")},
               "count": counts[h]} for h in hosts]
    index = {"generated": datetime.now().astimezone().isoformat(timespec="minutes"), "stores": stores, "games": games,
             "labels": classify.CFG["labels"], "search": classify.CFG["search"], "products": len(data["products"])}
    (out / "index.json").write_text(dump(index), "utf-8")


def write_page(name, query=""):
    page = (DIR / "report_template.html").read_text("utf-8").replace("__QUERY__", json.dumps(query, ensure_ascii=False))
    (SITE / f"{name}.html").write_text(page, "utf-8")


def build_report(data):
    write_site(data)
    write_page("index")
    print(f"Listo: {len(data['products'])} productos en {len(data['stores'])} tiendas -> {SITE / 'index.html'}")


def serve():
    """The page fetches its data, so file:// won't work; serve site/ locally."""
    import functools, http.server, webbrowser
    print("http://localhost:8000  (Ctrl+C para salir)")
    webbrowser.open("http://localhost:8000")
    http.server.ThreadingHTTPServer(("", 8000), functools.partial(http.server.SimpleHTTPRequestHandler, directory=SITE)).serve_forever()


if __name__ == "__main__":
    main()
