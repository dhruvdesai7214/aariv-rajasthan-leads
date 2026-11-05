"""Stage 3: fit score. Pilot: scores written by Claude in-session after reading the cached
homepage/about text (data/pilot_scores.csv). At scale: swap in a cheap-model API call per brand
using the same rubric (needs an API key).
Rubric: small founder-led + can pay (premium-ish pricing, professional site); hard 1-2 for
non-skincare, not trading, hijacked domain, services-only, sensitive-specialist."""
import csv
from common import read_csv, write_csv, DATA

def main():
    scores = {r["domain"]: r for r in csv.DictReader(open(DATA / "pilot_scores.csv", encoding="utf-8"))}
    rows = read_csv("filtered_brands.csv")
    out = []
    for r in rows:
        s = scores.get(r["domain"])
        if not s:
            continue
        r.update(fit_score=s["fit_score"], fit_reason=s["fit_reason"], category=s["category"])
        out.append(r)
    write_csv("stage3_all.csv", out, list(out[0].keys()))
    keep = [r for r in out if int(r["fit_score"]) >= 3]
    write_csv("scored_brands.csv", keep, list(out[0].keys()))

if __name__ == "__main__":
    main()
