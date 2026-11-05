# Indie skincare lead list (UK/US)

Pipeline that follows `BRIEF` stages 1-6. Scripts are in `pipeline/` and CSVs in `data/`.

```
cd pipeline
python3 stage1_formulabotanica.py        # source: Formula Botanica graduate portfolio (robots.txt allows)
python3 stage1_merge.py                  # -> data/raw_brands.csv (dedupe, exclude.txt, needs own domain)
python3 stage2_filter.py --ukus-hint     # -> data/stage2_all.csv (with drop_reason), data/filtered_brands.csv
python3 stage3_score.py                  # -> data/scored_brands.csv (pilot scores in data/pilot_scores.csv)
python3 stage4_5_founder_email.py        # -> data/stage4_5.csv
python3 stage6_report.py                 # -> data/final_list.csv + counts
```

Dependencies: `pip install requests gender-guesser english-words`.

Rules applied: robots.txt is respected. Instagram, LinkedIn, Facebook and Faire are never fetched
(Faire returns 403 to bots). Missing data stays blank. Every guessed email is `unverified_guess`
until a verifier key is added.
