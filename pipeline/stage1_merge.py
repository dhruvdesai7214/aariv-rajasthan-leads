"""Stage 1: merge sources -> raw_brands.csv (dedupe by domain, drop exclude.txt, drop no-domain)."""
from common import read_csv, write_csv, load_exclude, norm_name, domain_of, DATA

# Brands surfaced by manual web searches (query recorded), own-domain URL as found in results.
WEBSEARCH = [
    ("Glimmer Goddess Organic", "https://glimmergoddess.com", "small batch skincare brand US online store founder story"),
    ("Sunborn Organics", "https://sunbornorganics.com", "small batch skincare brand US online store founder story"),
    ("Taylor Made Organics", "https://taylormadeorganics.com", "small organic body care brand handmade US"),
    ("Toliver's Handmade", "https://tolivershandmade.com", "small organic body care brand handmade US"),
    ("DaisyFace", "https://www.daisyface.co", "small independent skincare brand UK founder-led"),
    ("Skin Cupid", "https://www.skincupid.us", "small independent skincare brand UK founder-led"),
]

def main():
    excl = load_exclude()
    rows = read_csv("raw_formulabotanica.csv")
    rows += [{"brand": b, "website": w, "source": "websearch", "query": q} for b, w, q in WEBSEARCH]
    out, seen, dropped = [], set(), {"no_domain": 0, "excluded": 0, "dupe": 0}
    for r in rows:
        if not r["website"]:
            dropped["no_domain"] += 1; continue
        if norm_name(r["brand"]) in excl:
            dropped["excluded"] += 1; continue
        d = domain_of(r["website"])
        if d in seen:
            dropped["dupe"] += 1; continue
        seen.add(d)
        r["domain"] = d
        out.append(r)
    print(dropped)
    write_csv("raw_brands.csv", out, ["brand", "website", "domain", "source", "query",
                                      "fb_founder", "fb_location", "fb_instagram"])

if __name__ == "__main__":
    main()
