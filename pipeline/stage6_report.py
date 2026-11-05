"""Stage 6: final_list.csv + report counts."""
import random, collections
from common import read_csv, write_csv

FINAL = ["brand", "website", "country", "category", "platform", "median_price", "instagram_handle",
         "founder_first", "founder_last", "founder_source", "email", "email_status", "fit_score", "fit_reason",
         # extras
         "email_pattern", "email_candidates", "generic_email", "mx", "founder_note", "product_count", "store_city",
         "has_stockists", "has_press", "has_wholesale_or_faire", "source"]

def main():
    rows = read_csv("stage4_5.csv")
    for r in rows:
        r["website"] = r.get("final_url") or r["website"]
    rows.sort(key=lambda r: (-int(r["fit_score"]), r["country"], r["brand"]))
    write_csv("final_list.csv", rows, FINAL)
    n = lambda f: len(read_csv(f))
    print("raw_brands:", n("raw_brands.csv"), "| stage2 checked:", n("stage2_all.csv"),
          "| filtered:", n("filtered_brands.csv"), "| scored>=3:", n("scored_brands.csv"), "| final:", len(rows))
    print(collections.Counter(r["email_status"] for r in rows))
    print("founder found:", sum(1 for r in rows if r["founder_first"]), "/", len(rows),
          "| full name:", sum(1 for r in rows if r["founder_last"]))
    print("country:", collections.Counter(r["country"] for r in rows))
    print("score:", collections.Counter(r["fit_score"] for r in rows))
    random.seed(7)
    for r in random.sample(rows, 10):
        print(" | ".join(r[k] for k in ("brand", "country", "median_price", "instagram_handle", "founder_first",
                                         "founder_last", "email", "email_status", "fit_score")))

if __name__ == "__main__":
    main()
