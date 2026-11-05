"""Stage 2: website checks (no AI). raw_brands.csv -> stage2_all.csv (every row + drop_reason)
and filtered_brands.csv (kept rows). Also caches homepage/about text in data/pages/ for stage 3/4."""
import json, re, statistics, sys, time, html as htmllib
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse
from common import get, LAST_ERROR, robots_ok, read_csv, write_csv, domain_of, DATA

PAGES = DATA / "pages"; PAGES.mkdir(exist_ok=True)
UKUS_HINTS = {"united kingdom", "uk", "england", "scotland", "wales", "northern ireland",
              "united states", "usa", "us", "united states of america"}
SPECIALIST_TITLE = re.compile(r"eczema|psoriasis|dermatitis|rosacea|sensitive skin|sensitive & |sensitive and ", re.I)
SPECIALIST_DESC = re.compile(r"eczema|psoriasis|dermatitis|rosacea|for sensitive skin|sensitive skin (care|specialist)", re.I)
MIN_PRODUCTS, MAX_PRODUCTS, MIN_MEDIAN = 4, 300, 10.0

def visible_text(h):
    h = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", h)
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", h))).strip()

def links(h, base):
    return [urljoin(base, m) for m in re.findall(r'href=["\']([^"\'#]+)', h)]

def shopify_products(base):
    prods = []
    for page in range(1, 5):
        r = get(f"{base}/products.json?limit=250&page={page}")
        if r is None or not r.ok or "application/json" not in r.headers.get("content-type", ""):
            return None if page == 1 else prods
        try:
            batch = r.json().get("products", [])
        except ValueError:
            return None if page == 1 else prods
        prods += batch
        if len(batch) < 250:
            break
    return prods

def woo_products(base):
    r = get(f"{base}/wp-json/wc/store/v1/products?per_page=100")
    if r is None or not r.ok:
        return None
    try:
        data = r.json()
    except ValueError:
        return None
    return data if isinstance(data, list) else None

def detect_currency(h, base, platform):
    m = re.search(r'Shopify\.currency\s*=\s*\{"active":"([A-Z]{3})"', h) or \
        re.search(r'"currency(?:Code)?"\s*:\s*"([A-Z]{3})"', h) or \
        re.search(r'og:price:currency"\s+content="([A-Z]{3})"', h) or \
        re.search(r'woocommerce_currency["\']?\s*[:=]\s*["\']([A-Z]{3})', h)
    if m:
        return m.group(1)
    if platform == "shopify":
        r = get(base + "/cart.js")
        if r is not None and r.ok:
            try:
                return r.json().get("currency", "")
            except ValueError:
                pass
    if "£" in h and "$" not in h: return "GBP"
    return ""

def find_page(h, base, words):
    for l in links(h, base):
        p = urlparse(l)
        if domain_of(l) == domain_of(base) and any(w in p.path.lower() for w in words):
            return l
    return ""

def check(row):
    out = dict(row)
    out.update(platform="", currency="", country="", median_price="", product_count="",
               instagram_handle=row.get("fb_instagram", ""), has_stockists="", has_press="",
               has_wholesale_or_faire="", has_careers="", about_url="", title="", drop_reason="")
    base = row["website"].rstrip("/")
    if not base.startswith("http"): base = "https://" + base
    r = get(base)
    if r is None or not r.ok or len(r.text) < 500:
        if r is None and LAST_ERROR.get(base) == "tls":
            out["drop_reason"] = "unreachable from this environment (TLS handshake via proxy) - recheck locally"
        elif r is None and not robots_ok(base):
            out["drop_reason"] = "robots.txt disallows (often a closed/password-protected Shopify store)"
        else:
            out["drop_reason"] = "site does not load / blocked"
        return out
    base = f"{urlparse(r.url).scheme}://{urlparse(r.url).netloc}"
    out["final_url"] = r.url
    if domain_of(r.url) != row["domain"] and any(x in domain_of(r.url) for x in ("etsy", "amazon", "linktr", "instagram")):
        out["drop_reason"] = "no own domain (redirects to marketplace)"; return out
    h = r.text
    low = h.lower()
    title = re.search(r"(?is)<title[^>]*>(.*?)</title>", h)
    out["title"] = visible_text(title.group(1))[:150] if title else ""
    desc = re.search(r'(?is)<meta[^>]+name="description"[^>]+content="([^"]*)"', h)
    desc = htmllib.unescape(desc.group(1)) if desc else ""

    # platform + products
    prices, count = [], None
    if "cdn.shopify.com" in low or "shopify" in low:
        prods = shopify_products(base)
        if prods is not None:
            out["platform"] = "shopify"; count = len(prods)
            mr = get(base + "/meta.json")
            try:
                meta = mr.json() if mr is not None and mr.ok else {}
            except ValueError:
                meta = {}
            out["currency"] = meta.get("currency", "")
            out["store_country"] = meta.get("country", "")
            out["store_city"] = meta.get("city", "")
            for p in prods:
                vs = [float(v["price"]) for v in p.get("variants", []) if v.get("price")]
                if vs: prices.append(min(vs))
    if not out["platform"] and ("woocommerce" in low or "wp-content" in low):
        prods = woo_products(base)
        out["platform"] = "woocommerce" if prods is not None or "woocommerce" in low else "wordpress"
        if prods:
            count = len(prods)
            for p in prods:
                pr = p.get("prices", {})
                try:
                    prices.append(int(pr["price"]) / 10 ** int(pr.get("currency_minor_unit", 2)))
                    out["currency"] = out["currency"] or pr.get("currency_code", "")
                except (KeyError, ValueError, TypeError):
                    pass
    if not out["platform"]:
        for name, marker in (("squarespace", "squarespace"), ("wix", "wix.com"), ("bigcommerce", "bigcommerce"),
                             ("bigcartel", "bigcartel"), ("ecwid", "ecwid"), ("webflow", "webflow")):
            if marker in low:
                out["platform"] = name; break
        else:
            out["platform"] = "other"
    prices = [p for p in prices if p > 0]
    if prices: out["median_price"] = round(statistics.median(prices), 2)
    if count is not None: out["product_count"] = count

    # country
    out["currency"] = out["currency"] or detect_currency(h, base, out["platform"])
    tld_uk = row["domain"].endswith(".uk")
    hint = (row.get("fb_location") or "").strip().lower()
    sc = out.get("store_country", "")
    if sc in ("GB", "US"): out["country"] = "UK" if sc == "GB" else "US"
    elif sc: out["country"] = "other:" + sc
    elif out["currency"] == "GBP" or tld_uk: out["country"] = "UK"
    elif out["currency"] == "USD": out["country"] = "US"
    elif out["currency"] and out["currency"] not in ("GBP", "USD"): out["country"] = "other:" + out["currency"]
    elif hint in UKUS_HINTS: out["country"] = "UK" if hint in {"united kingdom", "uk", "england", "scotland", "wales", "northern ireland"} else "US"
    elif hint: out["country"] = "other:" + hint

    # instagram from site (handle only, never visited)
    m = re.findall(r'instagram\.com/([A-Za-z0-9_.]{2,30})', h)
    junk = {"p", "explore", "accounts", "reel", "stories", "_u", "formulabotanica", "shopify", "wix", "squarespace"}
    m = [x.rstrip(".") for x in m if x.lower().rstrip(".") not in junk and len(x) >= 3]
    fbh = (row.get("fb_instagram") or "").lower()
    if fbh in junk: out["instagram_handle"] = ""
    if m:
        out["instagram_handle"] = next((x for x in m if x.lower() == fbh), m[0])

    # size signals
    ls = " ".join(l.lower() for l in links(h, base))
    out["has_stockists"] = "y" if re.search(r"stockist|where-to-buy|retailers", ls) else ""
    out["has_press"] = "y" if re.search(r"/press|/media|as-seen|in-the-press", ls) else ""
    out["has_wholesale_or_faire"] = "y" if re.search(r"wholesale|faire\.com", ls) else ""
    out["has_careers"] = "y" if re.search(r"careers|/jobs|join-the-team|work-with-us", ls) else ""
    out["about_url"] = find_page(h, base, ("about", "our-story", "story", "founder", "who-we-are"))

    about_text = ""
    if out["about_url"]:
        ar = get(out["about_url"])
        if ar is not None and ar.ok:
            about_text = visible_text(ar.text)
    home_text = visible_text(h)
    (PAGES / f"{row['domain']}.json").write_text(json.dumps(
        {"home": home_text[:6000], "about": about_text[:8000], "title": out["title"], "desc": desc}))

    # drop rules (first match wins)
    if not out["country"].startswith(("UK", "US")) or out["country"].startswith("other"):
        out["drop_reason"] = f"country not UK/US ({out['country'] or 'unknown'})"
    elif SPECIALIST_TITLE.search(out["title"]) or SPECIALIST_DESC.search(desc) or \
            len(re.findall(r"eczema|psoriasis|dermatitis", home_text, re.I)) >= 6:
        out["drop_reason"] = "eczema/sensitive specialist"
    elif count is not None and count < MIN_PRODUCTS:
        out["drop_reason"] = f"too few products ({count})"
    elif count is not None and count > MAX_PRODUCTS:
        out["drop_reason"] = f"too many products ({count})"
    elif out["median_price"] != "" and out["median_price"] < MIN_MEDIAN:
        out["drop_reason"] = f"median price too low ({out['median_price']})"
    elif out["has_careers"]:
        out["drop_reason"] = "careers page (probably too big)"
    return out

FIELDS = ["brand", "website", "domain", "source", "query", "fb_founder", "fb_location", "final_url",
          "platform", "currency", "country", "store_city", "median_price", "product_count", "instagram_handle",
          "has_stockists", "has_press", "has_wholesale_or_faire", "has_careers", "about_url", "title", "drop_reason"]

def main():
    rows = read_csv("raw_brands.csv")
    if "--ukus-hint" in sys.argv:   # pilot: skip brands whose listed location is clearly elsewhere
        rows = [r for r in rows if r["source"] != "formulabotanica_portfolio"
                or r["fb_location"].strip().lower() in UKUS_HINTS]
    if "--limit" in sys.argv:
        rows = rows[: int(sys.argv[sys.argv.index("--limit") + 1])]
    print(len(rows), "to check")
    with ThreadPoolExecutor(8) as ex:   # different domains in parallel; ~5 requests per domain
        res = list(ex.map(check, rows))
    write_csv("stage2_all.csv", res, FIELDS)
    write_csv("filtered_brands.csv", [r for r in res if not r["drop_reason"]], FIELDS)
    from collections import Counter
    print(Counter(r["drop_reason"].split(" (")[0] or "KEPT" for r in res))

if __name__ == "__main__":
    main()
