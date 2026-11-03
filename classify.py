"""Classify TCG products: game, language, kind, variant, plus a canonical group key so the same
product sold by different stores lines up. Vocabulary lives in aliases.json (edit it, don't touch the code).

  python classify.py --report    coverage + grouping quality over data.json
  python classify.py --test      self-check
"""
import collections, json, re, sys, unicodedata
from pathlib import Path

DIR = Path(__file__).parent
CFG = json.loads((DIR / "aliases.json").read_text("utf-8"))
GAMES = {g: re.compile(v["keywords"]) for g, v in CFG["games"].items()}  # dict order = priority
DROP = set(CFG["drop"])
PHRASES = CFG["phrases"]
PHRASE_RX = re.compile(r"\b(?:%s)\b" % "|".join(re.escape(p) for p in sorted(PHRASES, key=len, reverse=True)))

# Card ids found in titles: (regex, game hint, kind of id). "num" ids (NNN/NNN) repeat across sets, so the key adds the card name.
CODES = [
    (re.compile(r"\b((?:op|eb|prb|st)\d{2})-(\d{3})\b"), "onepiece", "set"),
    (re.compile(r"\b((?:fb|fs)\d{2})-(\d{3})\b"), "dragonball", "set"),
    (re.compile(r"\b((?:bt|ex|st)\d{1,2})-(\d{2,3})\b"), "digimon", "set"),
    (re.compile(r"\b([a-z0-9]{3,5})-(?:en|es|sp|jp|fr|de|it|pt|kr|ae|ap|sc)(\d{3})\b"), "yugioh", "set"),
    (re.compile(r"\b0*(\d{1,3})/0*(\d{2,3})\b"), None, "num"),
]
LANGS = [(l, re.compile(rx)) for l, rx in [
    ("es", r"espanol|spanish|\besp\b|\(es\)|\[es\]|-es\d"),
    ("en", r"ingles|english|\beng\b|\(en\)|\[en\]|-en\d|\ben\b"),
    ("ja", r"japones|japanese|\bjap\b|\bjp\b|\bjpn\b|\[jp\]|-jp\d"),
    ("ko", r"coreano|korean|\bkor\b|\bkr\b|-kr\d"),
    ("zh", r"chino|chinese|\bcn\b"),
    ("fr", r"frances|french|\bfra\b"),
    ("de", r"aleman|german|\bger\b"),
    ("it", r"italiano|italian"),
    ("pt", r"portugues|portuguese|\[por\]|-pt\d"),
]]
LANG_ANY = re.compile("|".join(rx.pattern for _, rx in LANGS))
COND = re.compile(r"\b(?:nm|lp|mp|hp|dmg|near mint|slightly played|moderately played|heavily played|damaged)\b")
# Specific first: each match is removed so "rare" can't match inside "ultra rare". Applied to singles only.
VARIANTS = [(v, re.compile(rx)) for v, rx in [
    ("special illustration rare", r"special illustration rare|\bsir\b"), ("illustration rare", r"illustration rare"),
    ("alt art", r"alternate art|alt\.? art|arte alternativ[oa]|parallel[a]?|\baa\b"), ("manga", r"\bmanga\b"),
    ("full art", r"full art"), ("secret rare", r"secret rare|\bsecreta\b"), ("ultra rare", r"ultra rare"),
    ("super rare", r"super rare"), ("double rare", r"double rare"), ("hyper rare", r"hyper rare"), ("ace spec", r"ace spec"),
    ("reverse holo", r"reverse holo(?:foil)?"), ("holo", r"\bholo(?:foil)?\b"), ("foil", r"\bfoil(?:eada)?\b"),
    ("borderless", r"borderless|sin bordes"), ("showcase", r"showcase"), ("extended art", r"extended art"),
    ("promo", r"\bpromo(?:cional)?\b"), ("rare", r"\brare\b|\brara\b"), ("uncommon", r"uncommon|poco comun"), ("common", r"\bcommon\b|\bcomun\b"),
]]
# First match wins (ported from the page). Strong single signals, accessories, sealed kinds, then weak single signals.
KINDS = [(k, re.compile(rx)) for k, rx in [
    ("single", r"\b\d{1,3}/\d{2,3}\b(?!/)|\b[a-z]{2,4}\d{1,2}-\d{3}\b|#\d+|\((?:alternate art|alt art|parallel|full art)\)|\b(?:psa|bgs|cgc) ?\d|reverse holo|\[holo\]"),
    ("accesorio", r"sleeves|\bmicas?\b|protectores|deck ?box|portamazo|playmat|tapete|toploader|top loader|carpeta|\balbum\b|\bbinder\b(?! collection)|\bdados\b|\bfundas?\b|peluche|figura|funko|llavero|rompecabezas|puzzle|polera|\btaza\b|mochila|stickers?|bolso"),
    ("booster box", r"booster box|\bdisplay\b|caja de sobres|caja de boosters|36 sobres"),
    ("etb", r"elite trainer|\betb\b|entrenador de elite"),
    ("bundle", r"bundle"), ("blister", r"blister|checklane|\d-pack"), ("tin", r"\btin\b|\blata\b"),
    ("sobre", r"booster pack|\bsobres?\b|sleeved booster|^booster\b"),
    ("coleccion", r"collection|coleccion|\bbox\b|\bcaja\b|\bdeck\b|\bmazo\b|premium|\bupc\b|battle academy"),
    ("single", r"\b[a-z]{1,4}\d{2,3}\b|\bex\b|\bvmax\b|\bvstar\b|\bgx\b|holo|promo|foil|\bnm\b"),
]]


def norm(s):
    s = s.lower().replace("’", "").replace("‘", "").replace("'", "")  # "ogre's" -> "ogres", not "ogre s"
    return unicodedata.normalize("NFD", s).encode("ascii", "ignore").decode()


def first_game(text):
    return next((g for g, rx in GAMES.items() if rx.search(text)), None)


def detect_lang(raw, n):
    n += " japanese" * bool(re.search("[぀-ヿ]", raw)) + " korean" * bool(re.search("[가-힯]", raw)) \
        + " chinese" * bool(re.search("[一-鿿]", raw) and not re.search("[぀-ヿ]", raw))
    found = [l for l, rx in LANGS if rx.search(n)]
    return None if not found else found[0] if len(found) == 1 else "multi"


def classify_one(title, cat=""):
    """Everything knowable from one product alone. Game may still be filled in by classify_all."""
    raw, n, c = title.lower(), norm(title), norm(cat or "")
    code = None
    for rx, hint, how in CODES:
        if m := rx.search(n):
            code = {"id": f"{int(m[1])}/{int(m[2])}" if how == "num" else f"{m[1]}-{m[2]}", "game": hint, "how": how, "m": m}
            break
    game, src = (first_game(c), "cat") if first_game(c) else (first_game(n), "title") if first_game(n) else (None, None)
    if not game and code and code["game"]:
        game, src = code["game"], "code"
    lang = detect_lang(raw, n) or detect_lang((cat or "").lower(), c)
    kind = "single" if code else next((k for k, rx in KINDS if rx.search(n)), "otro")

    s = n[:code["m"].start()] + " " + n[code["m"].end():] if code else n
    variants = []
    if kind == "single":
        for v, rx in VARIANTS:
            if rx.search(s):
                variants.append(v)
                s = rx.sub(" ", s)
    s = COND.sub(" ", LANG_ANY.sub(" ", s))
    s = PHRASE_RX.sub(lambda m: PHRASES[m[0]], s)
    gone = DROP | set(CFG["games"].get(game, {}).get("tokens", []))
    tokens = [t for t in re.findall(r"[a-z0-9]+", s) if t not in gone]
    return {"game": game, "src": src, "lang": lang, "kind": kind, "variant": ", ".join(sorted(variants)),
            "code": code and code["id"], "how": code and code["how"], "tokens": tokens}


def make_key(i):
    g, l = i["game"] or "?", i["lang"] or "?"
    if i["code"]:  # NNN/NNN repeats across sets: add the card's first word as a guard
        return f"{g}|{i['code']}|{i['tokens'][0] if i['how'] == 'num' and i['tokens'] else ''}|{l}"
    return f"{g}|{' '.join(sorted(set(i['tokens'])))}|{l}"


def classify_all(products):
    """products: {id: {title, store, cat?}} -> {id: info incl. key}. Two data-driven passes fill games the title doesn't name."""
    info = {k: classify_one(p["title"], p.get("cat") or "") for k, p in products.items()}
    # 1) Pokemon names learned from confidently-Pokemon numbered cards; unlabeled "Name 008/217" cards then match by name.
    names = collections.Counter(i["tokens"][0] for i in info.values() if i["game"] == "pokemon" and i["how"] == "num" and i["tokens"])
    known = {w for w, c in names.items() if c >= 3 and len(w) > 2}
    for i in info.values():
        if not i["game"] and i["how"] == "num" and i["tokens"] and i["tokens"][0] in known:
            i["game"], i["src"] = "pokemon", "name"
    # 2) A store where >=97% of >=500 classified items are one game: its unlabeled cards/sealed inherit it.
    # ponytail: mixed stores (most of them) stay unlabeled until crawler `cat` metadata arrives; 0.97 keeps their Magic out of Pokemon.
    per = collections.defaultdict(collections.Counter)
    for k, i in info.items():
        if i["game"]:
            per[products[k]["store"]][i["game"]] += 1
    default = {s: c.most_common(1)[0][0] for s, c in per.items() if sum(c.values()) >= 500 and c.most_common(1)[0][1] >= .97 * sum(c.values())}
    for k, i in info.items():
        if not i["game"] and i["kind"] not in ("accesorio", "otro") and products[k]["store"] in default:
            i["game"], i["src"] = default[products[k]["store"]], "store"
    for i in info.values():
        if i["kind"] == "otro" and i["game"]:  # a bare card name (Magic: "Counterspell") in a known game, with no sealed/accessory signal
            i["kind"] = "single"
        i["key"] = make_key(i)
    return info


def bucket(kind):
    return {"single": "single", "accesorio": "other", "otro": "other"}.get(kind, "sealed")


def build_groups(products, store_id):
    """-> {(game, bucket): [group]}. One group = one product across stores; offers cheapest (in stock) first.
    Compact keys for the page: t title, k kind, l language, i image, f first seen, o offers.
    Offer: s store index, p price, a in stock, u url (host-relative when possible), v variant, d previous price when it was higher."""
    info = classify_all(products)
    members = collections.defaultdict(list)
    for pid, p in products.items():
        if p["price"] > 0 and p["store"] in store_id:
            members[info[pid]["key"]].append((p, info[pid]))
    out = collections.defaultdict(list)
    for ms in members.values():
        offers, image = [], None
        for p, i in sorted(ms, key=lambda m: (not m[0]["available"], m[0]["price"])):
            prev = next((h[1] for h in reversed(p["history"]) if h[1] != p["price"]), 0)
            host = "https://" + p["store"]
            o = {"s": store_id[p["store"]], "p": round(p["price"]), "a": int(p["available"]),
                 "u": p["url"][len(host):] if p["url"].startswith(host + "/") else p["url"]}
            if i["variant"]:
                o["v"] = i["variant"]
            if prev > p["price"]:
                o["d"] = round(prev)
            offers.append(o)
            image = image or p.get("image")
        titles = collections.Counter(norm(p["title"]) for p, _ in ms)
        title = min((p["title"] for p, _ in ms), key=lambda t: (-titles[norm(t)], t.isupper(), len(t)))
        kind = collections.Counter(i["kind"] for _, i in ms).most_common(1)[0][0]
        game = ms[0][1]["game"] or "otros"
        out[(game, bucket(kind))].append({"t": title, "k": kind, "l": ms[0][1]["lang"] or "", "i": image or "",
                                          "f": min(p["first_seen"] for p, _ in ms)[:10], "o": offers})
    return out


def report():
    import time
    for attempt in range(5):  # data.json is rewritten once a minute while a crawl runs
        try:
            data = json.loads((DIR / "data.json").read_text("utf-8"))
            break
        except ValueError:
            time.sleep(5)
    prods = data["products"]
    info = classify_all(prods)
    n = len(info)
    pct = lambda x: f"{x:>7,} ({x / n:5.1%})"
    print(f"{n:,} productos\n")
    for title, f in [("Juego", lambda i: i["game"]), ("Origen del juego", lambda i: i["src"]), ("Idioma", lambda i: i["lang"]), ("Tipo", lambda i: i["kind"])]:
        print(f"== {title}")
        for k, c in collections.Counter(f(i) for i in info.values()).most_common():
            print(f"  {str(k):<14}{pct(c)}")
    groups = collections.defaultdict(list)
    for k, i in info.items():
        groups[i["key"]].append(k)
    stores_in = {key: {prods[k]["store"] for k in ks} for key, ks in groups.items()}
    multi = [key for key, s in stores_in.items() if len(s) >= 2]
    print(f"\n== Agrupacion\n  {len(groups):,} grupos; {len(multi):,} con 2+ tiendas; {sum(len(stores_in[k]) >= 3 for k in multi):,} con 3+")
    for key in sorted(multi, key=lambda k: -len(stores_in[k]))[:8]:
        print(f"  [{len(stores_in[key])} tiendas] {key}")
        for k in groups[key][:3]:
            print(f"      {prods[k]['store']}: {prods[k]['title']}")
    miss = [i for i in info.values() if not i["game"]]
    print(f"\n== Sin juego ({len(miss):,}): palabras mas comunes")
    print("  " + ", ".join(f"{w}({c})" for w, c in collections.Counter(t for i in miss for t in set(i["tokens"])).most_common(40)))
    import random
    random.seed(2)
    print("  muestra:", [prods[k]["title"] for k in random.sample([k for k, i in info.items() if not i["game"]], min(12, len(miss)))])


def test():
    c = classify_one
    a = c("Kuzan (OP02-121)")
    assert (a["game"], a["code"], a["kind"]) == ("onepiece", "op02-121", "single"), a
    a = c("Majespecter Unicorn - Kirin [BLGG-EN065] Ultra Rare")
    assert (a["game"], a["code"], a["lang"], a["variant"]) == ("yugioh", "blgg-065", "en", "ultra rare"), a
    a = c("Vaporeon ex - 023/131 - Double Rare (Español Foil NM)")
    assert (a["game"], a["lang"], a["code"], a["variant"]) == ("pokemon", "es", "23/131", "double rare, foil"), a
    assert c("Chikorita 008/217")["code"] == "8/217" and c("Chikorita 008/217")["game"] is None
    p = {"1": {"title": "Elite Trainer Box Perfect Order Pokémon TCG (Español)", "store": "a"},
         "2": {"title": "ETB Perfect Order - Pokemon - Español", "store": "b"},
         "3": {"title": "Charcadet Common Reverse Holo 019/094 Pokemon", "store": "a"},
         "4": {"title": "Charcadet - 019/094 - Common Pokemon", "store": "b"},
         "5": {"title": "Pokemon Booster Box Perfect Order Inglés", "store": "a"}}
    i = classify_all(p)
    assert i["1"]["key"] == i["2"]["key"], (i["1"], i["2"])
    assert i["3"]["key"] == i["4"]["key"], (i["3"], i["4"])
    assert i["5"]["key"] != i["1"]["key"]
    print("ok")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    report() if "--report" in sys.argv else test()
