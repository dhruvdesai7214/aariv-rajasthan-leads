"""Stage 4 (founder name) + Stage 5 (email candidates).
Founder: Formula Botanica profile name cross-checked against the brand's own About/home text,
or extracted from the site text. Never invented: blank when not found.
Email: emails published on the brand's own site (home/about/contact), MX check via DNS-over-HTTPS,
and pattern guesses. Without verifier keys every guess is marked 'unverified_guess'."""
import json, re, time
from urllib.parse import urljoin, urlparse
from common import get, session, read_csv, write_csv, domain_of, DATA
from stage2_filter import visible_text, find_page

NAME = r"([A-Z][a-zà-ÿ'’\-]+(?: [A-Z][a-zà-ÿ'’\-]+)?)"
SITE_PATTERNS = [
    r"[Ff]ounded by (?:Dr\.? |Dr )?" + NAME,
    r"[Ff]ounder[:,]? (?:Dr\.? )?" + NAME,
    r"[Ff]ounder (?:and|&) (?:[Ff]ormulator|CEO|[Cc]hemist|[Ee]sthetician)[:,]? (?:Dr\.? )?" + NAME,
    NAME + r",? (?:the |our |is the )?(?:[Cc]o-)?[Ff]ounder",
    r"(?:I'm|I’m|I am|[Mm]y name is) (?:Dr\.? )?" + NAME,
    r"[Mm]eet (?:Dr\.? )?" + NAME,
]
STOP = {"The", "Our", "We", "My", "Hi", "Hello", "Founder", "About", "Meet", "Skin", "Shop", "Story",
        "This", "Welcome", "In", "So", "A", "And", "Formulator", "Co", "Team", "Us", "Nature", "Behind"}
GENERIC = {"hello", "info", "contact", "hi", "support", "orders", "sales", "enquiries", "enquiry", "help",
           "customerservice", "care", "admin", "team", "shop", "wholesale", "press", "office", "mail", "service"}

COMMON = set("""skin skincare beauty organic organics natural naturals botanical botanicals body care co company
apothecary labs lab studio shop store collection products essentials oils oil balm butter soap face glow team story
founder founders about home our we us the and of for with in new york london farm farms herbal herbals pure clean
wellness spa clinic rituals ritual cosmetics creams cream serum serums love life world earth honey bee bees rose
garden wild moon sun golden gold shea tallow blog menu cart account login search news journal shop""".split())

import gender_guesser.detector as _gd
from english_words import get_english_words_set
_GENDER = _gd.Detector(case_sensitive=False)
_WORDS = get_english_words_set(["web2"], lower=True)

def plausible_first(tok):
    """Known first name (any country), or a capitalised token that is not an English word."""
    if not re.fullmatch(r"[A-Z][a-zà-ÿ'’\-]{1,}", tok) or tok.lower() in ("i'm", "i’m") or re.search(r"['’]s$", tok):
        return False
    if _GENDER.get_gender(tok) != "unknown":
        return True
    return tok.lower() not in _WORDS

def clean_name(n, brand=""):
    brand_words = {w.lower() for w in re.findall(r"[A-Za-z]+", brand)}
    parts = [p for p in n.split() if p not in STOP]
    parts = parts[:2]
    parts = [p.rstrip("-’'") for p in parts]
    if not parts or any(p.lower() in COMMON or len(p) < 2 for p in parts) or (len(parts) == 1 and parts[0].lower() in brand_words):
        return ""
    if not plausible_first(parts[0]):
        return ""
    if len(parts) == 2 and parts[1].lower() in _WORDS and _GENDER.get_gender(parts[1]) == "unknown" and \
            parts[1].lower() in ("nourishing", "natural", "organic", "handmade", "skincare", "beauty"):
        parts = parts[:1]
    return " ".join(parts)

def site_names(text, brand=""):
    found = []
    for pat in SITE_PATTERNS:
        for m in re.finditer(pat, text):
            n = clean_name(m.group(1), brand)
            if n and n not in found:
                found.append(n)
    return found

def mx(domain):
    try:
        r = session.get(f"https://dns.google/resolve?name={domain}&type=MX", timeout=10).json()
        return " ".join(sorted({a["data"].split()[-1].rstrip(".") for a in r.get("Answer", [])}))[:120]
    except Exception:
        return ""

def norm(s):
    import unicodedata
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower())

def patterns(first, last, d):
    f, l = norm(first), norm(last)
    if f and l:
        return [f"{f}@{d}", f"{f}.{l}@{d}", f"{f}{l}@{d}", f"{f[0]}.{l}@{d}", f"{f[0]}{l}@{d}", f"{f}_{l}@{d}"]
    return [f"{f}@{d}"] if f else []

import csv as _csv
OVERRIDES = {r["domain"]: r for r in _csv.DictReader(open(DATA / "founder_overrides.csv", encoding="utf-8"))}

def process(r):
    d = r["domain"]
    base = f"{urlparse(r.get('final_url') or r['website']).scheme or 'https'}://{urlparse(r.get('final_url') or r['website']).netloc}"
    pages = {}
    for key, url in (("home", base), ("about", r["about_url"])):
        if url:
            resp = get(url)
            if resp is not None and resp.ok:
                pages[key] = (url, resp.text)
    contact = find_page(pages.get("home", ("", ""))[1], base, ("contact",)) if "home" in pages else ""
    if contact:
        resp = get(contact)
        if resp is not None and resp.ok:
            pages["contact"] = (contact, resp.text)
    texts = {k: visible_text(h) for k, (u, h) in pages.items()}
    alltext = " ".join(texts.values())

    # --- Stage 4: founder
    first = last = src = note = ""
    fb = r.get("fb_founder", "").strip()
    fb = re.split(r"\s*(?:&|\band\b|,|/)\s*", fb)[0].strip() if fb else ""
    fb = re.sub(r"^(Dr\.?|Mrs?\.?|Ms\.?)\s+", "", fb)
    if fb:
        fparts = fb.split()
        on_site = [k for k in ("about", "home", "contact") if k in texts and re.search(r"\b" + re.escape(fparts[0]) + r"\b", texts[k])]
        first, last = fparts[0], (fparts[-1] if len(fparts) > 1 else "")
        if on_site:
            src = pages[on_site[0]][0]
            note = "name from Formula Botanica profile; first name confirmed on brand site"
        else:
            src = r["query"]
            note = "name from Formula Botanica graduate profile only (not found on brand site)"
    else:
        for k in ("about", "home"):
            names = site_names(texts.get(k, ""), r["brand"])
            if names:
                parts = names[0].split()
                first, last = parts[0], (parts[1] if len(parts) > 1 else "")
                src, note = pages[k][0], "extracted from brand site text"
                break

    ov = OVERRIDES.get(d)
    if ov:
        first, last, src, note = ov["founder_first"], ov["founder_last"], ov["founder_source"], "manual review: " + ov["founder_note"]

    # --- Stage 5: emails published on site
    html_all = " ".join(h for u, h in pages.values())
    emails = sorted({e.lower().rstrip(".") for e in re.findall(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", html_all)
                     if domain_of(e.split("@")[1]) == d or e.lower().endswith("@" + d)})
    emails = [e for e in emails if not re.search(r"\.(png|jpe?g|gif|webp|svg)$", e)]
    personal = [e for e in emails if e.split("@")[0] not in GENERIC]
    generic = [e for e in emails if e.split("@")[0] in GENERIC]
    cands = patterns(first, last, d)
    email, status, pattern = "", "", ""
    fnorm = norm(first)
    match = [e for e in personal if fnorm and e.split("@")[0].startswith(fnorm[:3])]
    if match:
        email, status = match[0], "published_on_site (not verifier-checked)"
        local = email.split("@")[0]
        for p, name in zip(cands, ["first@", "first.last@", "firstlast@", "f.last@", "flast@", "first_last@"]):
            if p.split("@")[0] == local: pattern = name
    elif cands:
        email, status, pattern = cands[0], "unverified_guess", "first@ (guess)"
    elif generic:
        email, status = generic[0], "generic_inbox_last_resort"
    mxh = mx(d)
    if not mxh and status:
        status += "; NO MX RECORD"
    return {**r, "founder_first": first, "founder_last": last, "founder_source": src, "founder_note": note,
            "site_emails": " ".join(emails), "generic_email": generic[0] if generic else "",
            "email": email, "email_status": status, "email_pattern": pattern,
            "email_candidates": " ".join(cands), "mx": mxh}

def main():
    from concurrent.futures import ThreadPoolExecutor
    rows = read_csv("scored_brands.csv")
    with ThreadPoolExecutor(16) as ex:
        out = list(ex.map(process, rows))
    write_csv("stage4_5.csv", out, list(out[0].keys()))

if __name__ == "__main__":
    main()
