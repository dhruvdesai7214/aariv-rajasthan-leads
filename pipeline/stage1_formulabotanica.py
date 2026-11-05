"""Stage 1 source: Formula Botanica graduate brand portfolio (robots.txt allows).
Each page lists brand name, founder, location, online store URL and Instagram."""
import html, re, sys, time
from common import get, write_csv, domain_of

NON_BRAND_HOSTS = ("formulabotanica", "etsy.com", "folksy.com", "amazon.", "notonthehighstreet",
                   "facebook.com", "instagram.com", "linktr.ee", "wolfandbadger", "faire.com")

def text(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()

def parse(url, h):
    def field(label):
        m = re.search(r'<strong[^>]*>\s*' + label + r'\s*:?\s*</strong>\s*:?\s*([^<]*)', h, re.I)
        return text(m.group(1)) if m else ""
    name = re.search(r'<h2 class="green">(.*?)</h2>', h, re.S)
    store = re.search(r'Online Store</h3>.*?<a[^>]+href="([^"]+)"', h, re.S)
    ig = re.search(r'href="https?://(?:www\.)?instagram\.com/([A-Za-z0-9_.]+)', h)
    site = store.group(1).strip() if store else ""
    if any(b in site.lower() for b in NON_BRAND_HOSTS):
        site = ""   # no own domain
    return {
        "brand": text(name.group(1)) if name else "",
        "website": site,
        "source": "formulabotanica_portfolio",
        "query": url,
        "fb_founder": field("founder"),
        "fb_location": field("location"),
        "fb_instagram": ig.group(1) if ig else "",
    }

def main():
    urls = []
    for n in (1, 2):
        r = get(f"https://formulabotanica.com/portfolio-sitemap{n}.xml")
        urls += re.findall(r"<loc>(https://formulabotanica.com/portfolio/[^<]+)</loc>", r.text)
    urls = sorted(set(urls))
    print(len(urls), "portfolio pages")
    rows = []
    for i, u in enumerate(urls):
        r = get(u)
        if r is not None and r.ok:
            rows.append(parse(u, r.text))
        time.sleep(0.5)
        if i % 50 == 0:
            print(i, file=sys.stderr)
    write_csv("raw_formulabotanica.csv", rows,
              ["brand", "website", "source", "query", "fb_founder", "fb_location", "fb_instagram"])

if __name__ == "__main__":
    main()
