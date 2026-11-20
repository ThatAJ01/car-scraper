import json, queue, re, threading, time, unicodedata, uuid
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, Response, jsonify, request, send_from_directory

ROOT = Path(__file__).parent
SITES_FILE = ROOT / "sites.json"
app = Flask(__name__, static_folder=str(ROOT / "static"))

MIN_PRICE = 50000  # with "hide parts" on, anything cheaper is treated as parts/accessories
EUR_CZK = 25  # rough rate, only used for listings priced in €
MAX_PAGES = 5000  # safety cap on "next page" links followed per site (effectively: all of them)
SITE_BUDGET_S = 600  # safety cap: stop paging a site after 10 minutes. The Stop button ends it sooner.

BRANDS = ["Alfa Romeo", "Audi", "BMW", "Chevrolet", "Citroen", "Cupra", "Dacia", "Fiat", "Ford", "Honda",
          "Hyundai", "Jaguar", "Jeep", "Kia", "Land Rover", "Lexus", "Mazda", "Mercedes", "Mini",
          "Mitsubishi", "Nissan", "Opel", "Peugeot", "Porsche", "Renault", "Seat", "Skoda", "Smart",
          "Subaru", "Suzuki", "Tesla", "Toyota", "Volkswagen", "Volvo"]
BRAND_ALIASES = {"vw": "Volkswagen", "mercedes-benz": "Mercedes", "škoda": "Skoda", "citroën": "Citroen"}

# keyword -> normalized value; first hit wins. Keywords of <=3 chars must match a whole word.
FUEL = [("hybrid", "hybrid"), ("hybridní", "hybrid"), ("plug-in", "hybrid"),
        ("elektro", "electric"), ("electric", "electric"),
        ("nafta", "diesel"), ("diesel", "diesel"), ("naftový", "diesel"), ("tdi", "diesel"), ("crdi", "diesel"), ("hdi", "diesel"),
        ("benzín", "petrol"), ("benzin", "petrol"), ("petrol", "petrol"), ("tsi", "petrol"), ("tfsi", "petrol")]
GEARBOX = [("automat", "automatic"), ("dsg", "automatic"), ("tiptronic", "automatic"), ("cvt", "automatic"),
           ("manuál", "manual"), ("manual", "manual"), ("mechanick", "manual")]
BODY = [("suv", "suv"), ("crossover", "suv"), ("terénní", "suv"), ("sedan", "sedan"), ("limuzína", "sedan"),
        ("hatchback", "hatchback"), ("liftback", "hatchback"), ("kombi", "wagon"), ("combi", "wagon"),
        ("kupé", "coupe"), ("coupe", "coupe"), ("kabriolet", "convertible"), ("cabrio", "convertible"),
        ("minivan", "van"), ("mpv", "van"), ("dodávka", "van"), ("van", "van")]

PRICE_RE = re.compile(r"(\d{1,3}(?:[\s .]\d{3})+|\d{4,})\s*(Kč|CZK|,-|€|EUR)", re.I)


DEFAULT_SITES = [{"id": "sauto", "name": "sauto.cz", "url": "https://www.sauto.cz/inzerce/osobni", "enabled": True},
                 {"id": "bazos", "name": "auto.bazos.cz", "url": "https://auto.bazos.cz/", "enabled": True}]


def load_sites():
    if not SITES_FILE.exists():
        save_sites(DEFAULT_SITES)  # first run
    return json.loads(SITES_FILE.read_text("utf-8"))


def save_sites(sites):
    SITES_FILE.write_text(json.dumps(sites, indent=2, ensure_ascii=False), "utf-8")


# ---------- reading a listing ----------
def pick(text, table):
    t = text.lower()
    t = re.sub(r"(automat\w*|manu\w+)\s+klimat\w*", "", t)  # "automatická klimatizace" is not a gearbox
    for kw, val in table:
        pat = r"(?<![^\W\d_])" + re.escape(kw) + (r"(?![^\W\d_])" if len(kw) <= 3 else "")
        if re.search(pat, t):
            return val
    return None


def parse_price(text):
    """First price >= 20 000 Kč in text (skips monthly instalments), else the biggest. Returns CZK int."""
    found = []
    for num, cur in PRICE_RE.findall(text):
        v = int(re.sub(r"\D", "", num))
        found.append(v * EUR_CZK if cur.lower() in ("€", "eur") else v)
    return next((v for v in found if v >= 20000), max(found, default=None))


KM_NUM = r"(\d{1,3}(?:[ .,']\d{3})+|\d+)"


def parse_km(t):
    """Mileage: the number after a label like "Najeto:" if there is one, else the biggest "N km" / "N tis. km" / "Ntkm".
    Smaller ones are usually service intervals ("rozvody před 30 tis km") or consumption ("100 km")."""
    def ok(k):
        return k >= 1000 and not 1990 <= k <= 2030  # also drops a year read as km
    t = re.sub(r"(?<!\d)(\d{1,3})[ .,']?[xX]{3}(?![A-Za-z])", r"\1 000", t)  # "260 xxx km" = roughly 260 000 km
    m = re.search(r"(?:najeto|najete|najet|najezd|ujeto|tachometr\w*)(?:\W|jen|cca|asi|pouze){0,12}" + KM_NUM + r"(?:\s*(tis|tkm))?", norm(t))
    if m:
        k = int(re.sub(r"\D", "", m.group(1))) * (1000 if m.group(2) else 1)
        if ok(k):
            return k
    cands = [int(re.sub(r"\D", "", m.group(1))) for m in re.finditer(r"(?<!\d)" + KM_NUM + r"\s*km\b", t, re.I)]
    cands += [int(float(m.group(1).replace(",", ".")) * 1000)
              for m in re.finditer(r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(?:tis\.?\s*km|tkm)", t, re.I)]
    return max((k for k in cands if ok(k)), default=None)


def parse_year(t):
    """Year after a label ("Rok: 2010", "r.v. 2016"), else the first year in the text (posting dates removed)."""
    m = re.search(r"(?:rok vyroby|rok|r\.? ?v\.?|vyrobeno)\W{0,6}((?:19[89]\d|20[0-2]\d))(?!\d)", norm(t))
    if m:
        return int(m.group(1))
    dateless = re.sub(r"\d{1,2}\.\s*\d{1,2}\.\s*(19|20)\d\d", "", t)  # "3.5. 2026" is a date, not the car's year
    return next((int(y) for y in re.findall(r"(?<!\d)(19[89]\d|20[0-2]\d)(?!\d)", dateless)), None)


def parse_car(text):
    t = text.replace("\xa0", " ")
    year = parse_year(t)
    km = parse_km(t)
    low = t.lower()
    brand = next((n for a, n in BRAND_ALIASES.items() if re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", low)), None) \
        or next((b for b in BRANDS if re.search(r"(?<!\w)" + b.lower() + r"(?!\w)", low)), None)
    return {"brand": brand, "year": year, "km": km, "price": parse_price(t),
            "fuel": pick(t, FUEL), "gearbox": pick(t, GEARBOX), "body": pick(t, BODY)}


# ---------- finding listings on any page, no per-site config ----------
def sig(el):
    return el.name + "." + ".".join(sorted(el.get("class", [])))


def find_cards(soup):
    """Cards = repeated elements (same tag+classes) that each hold a link and a price. Outermost group wins."""
    for t in soup(["script", "style", "noscript", "head"]):
        t.decompose()
    groups = defaultdict(dict)  # signature -> {element id: element}
    for node in soup.find_all(string=PRICE_RE):
        el = node.parent
        for _ in range(8):
            if el is None or el.name in ("body", "html"):
                break
            if el.find("a", href=True) or el.name == "a":
                groups[sig(el)][id(el)] = el
            el = el.parent
    best, best_key = [], (0, 0)
    for els in groups.values():
        els = list(els.values())
        if len(els) < 3 or any(len(PRICE_RE.findall(e.get_text(" "))) > 4 for e in els[:3]):
            continue
        key = (len(els), sum(len(e.get_text()) for e in els) / len(els))
        # prefer more cards; among near-equal counts prefer the outer (longer text) container
        if key[0] > best_key[0] * 1.2 or (key[0] >= best_key[0] * 0.8 and key[1] > best_key[1]):
            best, best_key = els, key
    return best


def read_card(el, base):
    links = ([el] if el.name == "a" and el.get("href") else []) + el.find_all("a", href=True)
    link = max(links, key=lambda a: len(a.get_text(strip=True)))
    heading = el.find(["h1", "h2", "h3", "h4"])
    title = (heading or link).get_text(" ", strip=True) or link.get("title", "")
    img = el.find("img")
    src = (img.get("src") or img.get("data-src") or img.get("data-lazy-src") or "") if img else ""
    text = el.get_text(" ", strip=True)
    car = parse_car(text)
    # the real price sits in its own element ("499 000 Kč"); prices inside the description text ("bonus 50 000 Kč") don't count
    own = [s.strip() for s in el.find_all(string=PRICE_RE) if PRICE_RE.fullmatch(s.strip())]
    if own:
        car["price"] = parse_price(" ".join(own))
    car.update(title=title[:120], link=urljoin(base, link["href"]), image=urljoin(base, src) if src and not src.startswith("data:") else "")
    return car


def page_texts(soup):
    """(summary, body) text of an ad page. Summary = title + JSON-LD + meta descriptions (about this ad only);
    body = visible text without menus/footers, capped, because it can also contain "similar ads"."""
    head = [soup.h1.get_text(" ", strip=True)] if soup.h1 else []
    for t in soup.find_all("script", type="application/ld+json"):
        try:
            stack = [json.loads(t.string or "")]
        except ValueError:
            continue
        while stack:
            d = stack.pop()
            if isinstance(d, dict):
                head += [v for k, v in d.items() if k in ("name", "description") and isinstance(v, str)]
                stack += [v for v in d.values() if isinstance(v, (dict, list))]
            elif isinstance(d, list):
                stack += d
    for m in soup.find_all("meta"):
        if (m.get("name") or m.get("property")) in ("description", "og:title", "og:description") and m.get("content"):
            head.append(m["content"])
    for t in soup(["script", "style", "noscript", "nav", "footer", "aside", "form"]):
        t.decompose()
    return " ".join(head).replace("\xa0", " "), soup.get_text(" ", strip=True)[:6000].replace("\xa0", " ")


def enrich(c, stop):
    """Open the ad itself and fill km/year/fuel/gearbox/body from the full text (summary first, then body,
    then what the search card said). Any failure keeps the card data."""
    if stop.is_set():
        return c
    try:
        r = requests.get(c["link"], timeout=(5, 10), headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "cs,en;q=0.8"})
        r.raise_for_status()
        head, body = page_texts(BeautifulSoup(r.text, "html.parser"))
    except Exception:
        return c
    h, b = parse_car(head), parse_car(body)
    for k in ("km", "year", "fuel", "gearbox", "body"):
        c[k] = next((v for v in (h[k], b[k], c[k]) if v is not None), None)
    return c


def next_url(soup, base):
    a = soup.find("a", rel=lambda r: r and "next" in r, href=True) or \
        soup.select_one('a[class*="next"][href]') or \
        soup.find("a", href=True, string=re.compile(r"^\s*(další|následující|next|›|»)\s*$", re.I))
    return urljoin(base, a["href"]) if a else None


def scrape_site(site, emit, stop):
    """Walk every page of the site; call emit(new_cars) after each page. Ends when there is no next page,
    the page adds nothing new, the time budget runs out, or `stop` is set."""
    url, seen = site["url"], set()
    deadline = time.monotonic() + SITE_BUDGET_S
    for _ in range(MAX_PAGES):
        if stop.is_set() or (seen and time.monotonic() > deadline):
            break
        try:
            r = requests.get(url, timeout=(5, 10), headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "cs,en;q=0.8"})
            r.raise_for_status()
        except requests.RequestException as e:
            if seen:
                break  # a later page failed, keep earlier pages
            raise ValueError(f"could not load the page: {type(e).__name__}") from e
        soup = BeautifulSoup(r.text, "html.parser")
        nxt = next_url(soup, r.url)
        new = []
        for el in find_cards(soup):
            c = read_card(el, r.url)
            if c["link"] not in seen:
                seen.add(c["link"])
                c["site"] = site["name"]
                new.append(c)
        if not new:
            if not seen:
                raise ValueError("no car listings found on this page (it may need JavaScript, or block scrapers)")
            break  # page added nothing new: pagination is looping or finished
        emit(new)
        if not nxt or nxt in (url, r.url):
            break
        url = nxt


PARTS = re.compile(r"(?<![a-z])(dil|dily|nahradni|na dily|kolo|kola|disk|disky|pneu\w*|alu|svetlo|svetla|svetel|reflektor\w*|"
                   r"naraznik\w*|blatnik\w*|zrcatko|zrcatka|kapota|dvere|dverni|sedacky|sedadla|volant|airbag\w*|autoradio|"
                   r"vlecne|nosic\w*|strešni|stresni|kryt\w*|koberce|koberec|sada|sady|motor|prevodovka|turbo|"
                   r"vyfuk\w*|tlumic\w*|zadni sklo|cela sklo|maska|mrizka|lemy|spoiler|ostatni|prislusenstvi)(?![a-z])")


def is_part(c):
    """Spare part / accessory rather than a whole car (no mileage listed, and either a tiny price
    or a parts word in the title with a price under 40 000 Kč)."""
    if c["km"] is not None:
        return False
    p, title = c["price"], norm(c["title"])
    if re.search(r"(?<![a-z])(dil|dily|nahradni|na dily|pneu\w*|disk|disky)(?![a-z])", title):
        return True  # clearly parts, whatever the price
    return (p is not None and p < 15000) or (bool(PARTS.search(title)) and (p is None or p < 40000))


def norm(s):
    """lowercase, no diacritics: 'Škoda' -> 'skoda'"""
    return "".join(ch for ch in unicodedata.normalize("NFD", (s or "").lower()) if not unicodedata.combining(ch))


def matches(c, f):
    def num(k):
        v = str(f.get(k, "")).strip()
        return int(v) if v.isdigit() else None
    if f.get("hide_parts") and (is_part(c) or (c["price"] is not None and c["price"] < MIN_PRICE)):
        return False
    title = norm(c["title"])
    b = norm(f.get("brand", "").strip())
    if b and norm(c["brand"]) != b and b not in title:
        return False
    if any(w not in title for w in norm(f.get("model", "")).split()):  # "octavia" or "octavia combi"
        return False
    for key, field, op in (("km_max", "km", max), ("year_min", "year", min), ("year_max", "year", max), ("price_max", "price", max)):
        lim = num(key)
        if lim is not None and c[field] is not None:
            if (op is max and c[field] > lim) or (op is min and c[field] < lim):
                return False
    for k in ("fuel", "gearbox", "body"):
        if f.get(k) and c[k] and c[k] != f[k]:
            return False
    return True


# ---------- routes ----------
@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/sites")
def get_sites():
    return jsonify(load_sites())


@app.post("/api/sites")
def add_site():
    url = (request.get_json() or {}).get("url", "").strip()
    if not url:
        return jsonify(error="paste a URL"), 400
    if not re.match(r"https?://", url):
        url = "https://" + url
    host = urlparse(url).netloc
    if "." not in host and not host.startswith("127.0.0.1"):
        return jsonify(error="that doesn't look like a URL"), 400
    s = {"id": uuid.uuid4().hex[:8], "name": host.removeprefix("www."), "url": url, "enabled": True}
    save_sites(load_sites() + [s])
    return jsonify(s)


@app.patch("/api/sites/<sid>")
def toggle_site(sid):
    sites = load_sites()
    for s in sites:
        if s["id"] == sid:
            s["enabled"] = bool(request.get_json().get("enabled"))
    save_sites(sites)
    return jsonify(ok=True)


@app.delete("/api/sites/<sid>")
def del_site(sid):
    save_sites([s for s in load_sites() if s["id"] != sid])
    return jsonify(ok=True)


@app.post("/api/search")
def search():
    """Streams newline-delimited JSON: {"site","scraped","cars":[matching new cars]} per page,
    {"site","error"} on failure, {"done":true} at the end. Closing the page stops the scraping."""
    f = request.get_json() or {}
    sites = [s for s in load_sites() if s.get("enabled")]
    q, stop, scraped = queue.Queue(), threading.Event(), defaultdict(int)

    def run(s):
        def emit(cars):
            scraped[s["name"]] += len(cars)
            cars = [c for c in cars if matches(c, f)]  # cheap check on the search card first
            if f.get("full_ad") and cars:
                with ThreadPoolExecutor(max_workers=6) as ex:
                    cars = [c for c in ex.map(lambda c: enrich(c, stop), cars) if matches(c, f)]  # then on the whole ad
            q.put({"site": s["name"], "scraped": scraped[s["name"]], "cars": cars})
        try:
            scrape_site(s, emit, stop)
        except Exception as e:  # report per-site, keep others going
            q.put({"site": s["name"], "error": str(e)})
        q.put({"site_done": s["name"]})

    def stream():
        for s in sites:
            threading.Thread(target=run, args=(s,), daemon=True).start()
        left = len(sites)
        try:
            while left:
                msg = q.get()
                left -= "site_done" in msg
                yield json.dumps(msg) + "\n"
            yield json.dumps({"done": True}) + "\n"
        finally:
            stop.set()  # browser closed or pressed Stop

    return Response(stream(), mimetype="application/x-ndjson")


# ---- fake listing page, handy for testing: add http://127.0.0.1:5000/demo as a site ----
DEMO = [("Volkswagen Golf 1.6 TDI", 2016, 142000, "Nafta", "Manuální", "Hatchback", 189000),
        ("BMW 320d Touring", 2018, 98000, "Nafta", "Automatická", "Kombi", 429000),
        ("Toyota Yaris Hybrid", 2020, 45000, "Hybridní", "Automatická", "Hatchback", 379000),
        ("Tesla Model 3", 2021, 38000, "Elektro", "Automatická", "Sedan", 749000),
        ("Škoda Octavia Combi 1.5 TSI", 2019, 76000, "Benzín", "Manuální", "Kombi", 399000),
        ("Kia Sportage 1.6 CRDi", 2017, 121000, "Nafta", "Manuální", "SUV", 339000)]


@app.get("/demo")
def demo():
    rows = "".join(
        f'<div class="car"><a href="/demo#{i}"><b>{t}</b></a><span>{p:,} Kč</span>'
        f'<p>{y} · {km:,} km · {fu} · {g} · {b}</p></div>'.replace(",", " ")
        for i, (t, y, km, fu, g, b, p) in enumerate(DEMO))
    return f"<html><body>{rows}</body></html>"


if __name__ == "__main__":
    app.run(port=5000)
