"""Stage 3: fit score 1-5 (keep >= 3).
Pilot brands use the hand-reviewed scores in data/pilot_scores.csv.
All other brands get a transparent rule-based score from the cached homepage + about text and the
stage 2 signals. (Swap in a cheap-model call per brand when an API key is available.)"""
import csv, json, re
from common import read_csv, write_csv, DATA

DEAD = re.compile(r"enter (the )?password|opening soon|coming soon|not currently trading|domain (is )?for sale|"
                  r"website (is )?under (construction|maintenance)|store (is )?closed|we are closing|closing down", re.I)
HIJACK = re.compile(r"\b(togel|slot gacor|gacor|casino|judi|sbobet|toto|bandar|poker online|hugedomains)\b", re.I)
NON_SKIN_TITLE = re.compile(r"\b(hair ?care|haircare|shampoo|candles?|home fragrance|wax melts|perfume|parfum|"
                            r"jewel|clothing|makeup|cosmetics? manufacturer|private label|white label|clinic|salon|"
                            r"spa\b|facials? in|treatments? in|massage|reflexology|academy|training|course)", re.I)
SKIN = re.compile(r"skin ?care|skincare|serum|moisturi[sz]er|face oil|cleanser|balm|body butter|body oil|spf|sunscreen|toner", re.I)
FOUNDER = re.compile(r"\bfounder|\bfounded by|\bI['’]m [A-Z][a-z]+|\bmy name is|\bmeet [A-Z][a-z]+|co-?founder", re.I)
MARKET = re.compile(r"multivendor|marketplace|shop (all )?brands|our brands|brands we stock|stockist of", re.I)

def score(r, page):
    home, about, title, desc = page.get("home", ""), page.get("about", ""), page.get("title", ""), page.get("desc", "")
    text = f"{title} {desc} {home[:4000]} {about[:4000]}"
    reasons = []
    if DEAD.search(f"{title} {home[:1500]}"):
        return 1, "store closed / password page / coming soon", "other"
    if HIJACK.search(f"{title} {desc} {home[:2000]}"):
        return 1, "domain no longer a skincare brand (hijacked/parked)", "other"
    if MARKET.search(f"{title} {desc}"):
        return 1, "multi-brand shop / marketplace, not a brand", "other"
    if NON_SKIN_TITLE.search(f"{title} {desc}") and not SKIN.search(f"{title} {desc}"):
        return 2, "title/description not skincare-led (hair/candles/services/manufacturer)", "other"
    if not SKIN.search(text):
        return 2, "no skincare products evident on homepage", "other"
    s = 3.0
    price = float(r["median_price"]) if r.get("median_price") else None
    n = int(r["product_count"]) if r.get("product_count") else None
    if price is not None:
        if price >= 30: s += 1; reasons.append(f"premium-ish pricing (median {price:g})")
        elif price < 15: s -= 1; reasons.append(f"low price point (median {price:g})")
        else: reasons.append(f"mid pricing (median {price:g})")
    else:
        reasons.append("no price data")
    if FOUNDER.search(f"{about} {home}"):
        s += 0.5; reasons.append("founder story on site")
    else:
        s -= 0.5; reasons.append("no founder story found")
    if n is not None:
        if n > 150: s -= 1; reasons.append(f"{n} products (maybe too big)")
        elif n <= 6: s -= 0.5; reasons.append(f"only {n} products")
        else: reasons.append(f"{n} products")
    if r.get("has_press") or r.get("has_stockists") or r.get("has_wholesale_or_faire"):
        s += 0.5; reasons.append("press/stockists/wholesale (real business)")
    if r.get("platform") in ("wix", "other", "wordpress") and price is None:
        s -= 0.5; reasons.append(f"{r['platform']} site, couldn't verify shop")
    s = max(1, min(5, round(s)))
    cat = category(text)
    return s, "; ".join(reasons), cat

CATS = [("men's", r"\bmen'?s\b|for men|beard|grooming"), ("acne", r"\bacne|blemish|breakout"),
        ("anti-ageing", r"anti-?ag(e)?ing|mature skin|menopaus|wrinkle|fine lines|pro-?age"),
        ("SPF", r"\bspf\b|sunscreen|suncare"), ("baby", r"\bbaby\b|babies|toddler"),
        ("body care", r"body butter|body oil|body scrub|bodycare|body care|deodorant"),
        ("natural/organic", r"organic|natural|botanical|plant-based|herbal")]

def category(text):
    for name, pat in CATS:
        if re.search(pat, text[:6000], re.I):
            return name
    return "skincare"

def main():
    manual = {r["domain"]: r for r in csv.DictReader(open(DATA / "pilot_scores.csv", encoding="utf-8"))}
    out = []
    for r in read_csv("filtered_brands.csv"):
        if r["domain"] in manual:
            m = manual[r["domain"]]
            r.update(fit_score=m["fit_score"], fit_reason=m["fit_reason"], category=m["category"], score_method="manual")
        else:
            p = DATA / "pages" / f"{r['domain']}.json"
            page = json.loads(p.read_text()) if p.exists() else {}
            s, why, cat = score(r, page)
            r.update(fit_score=s, fit_reason=why, category=cat, score_method="rules")
        out.append(r)
    fields = list(out[0].keys())
    write_csv("stage3_all.csv", out, fields)
    write_csv("scored_brands.csv", [r for r in out if int(r["fit_score"]) >= 3], fields)
    from collections import Counter
    print(Counter(str(r["fit_score"]) for r in out))

if __name__ == "__main__":
    main()
