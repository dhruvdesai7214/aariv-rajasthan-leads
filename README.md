# aariv-rajasthan-leads

Lead generation pipeline for **Aariv Fabrics (Ahmedabad)**, a supplier of dyed and laminated jute rolls.
It finds small-to-mid bag manufacturers in Rajasthan (Jaipur, Jodhpur, Kishangarh, Udaipur, Ajmer,
Bikaner) that make jute, juco, cotton canvas or promotional bags and buy jute fabric as raw material.
The output is a CSV the sales team can use for cold outreach.

## Pipeline

| Step | Script | Output |
|---|---|---|
| 1. Google Maps search | `src/scrape_gmaps.py` (Apify `compass/crawler-google-places`) | `data/raw/gmaps/<query_slug>.json` |
| 2. Dedupe | `src/dedupe.py` (place_id, then rapidfuzz name match ≥ 85 within a city) | `data/intermediate/companies_deduped.csv` |
| 3. Websites | `src/scrape_websites.py` (requests + BeautifulSoup; Firecrawl fallback) | `data/raw/websites/<domain>.md` |
| 4. Extract | `src/extract.py` (rule-based by default; Claude optional) | `data/enriched/<domain>.json` |
| 5. Merge | `src/merge.py` | `data/final/aariv_rajasthan_leads.csv`, `..._tier_a.csv` |

Every step is **resumable**: it skips work whose output file already exists, so you can re-run the same
command after an interruption. Logs go to `runs/<timestamp>.log`.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # add -r requirements-dev.txt for tests
cp .env.example .env                      # then fill in the keys
```

| Key | Needed for | Cost |
|---|---|---|
| `APIFY_API_TOKEN` | Step 1 | Free plan includes ~$5/month of credit. The actor charges ~$0.004/place, so the default config (33 queries × 35 places ≈ 1,155 places ≈ $4.60) fits. All paid add-ons are switched off and each run has a spend cap (`max_charge_usd_per_query`). |
| `FIRECRAWL_API_KEY` | Step 3 fallback only | Free tier credits. Only used when a page times out, is blocked, or has no text (JS-rendered). Capped by `--max-firecrawl-pages` (default 150). Optional: without it those pages are skipped. |
| `ANTHROPIC_API_KEY` | Step 4 with `--use-llm` only | Paid (Claude `claude-sonnet-5-5`). **Not needed for the default free run.** |

## Run

```bash
# Everything, for three cities
python -m src.pipeline --cities jaipur,jodhpur,kishangarh

# All six configured cities
python -m src.pipeline

# Only some steps (e.g. re-merge after editing enrichment)
python -m src.pipeline --steps extract,merge

# Each step also runs on its own
python -m src.scrape_gmaps --cities jaipur
python -m src.dedupe
python -m src.scrape_websites --retry-failed
python -m src.extract            # free, rule-based
python -m src.extract --use-llm  # paid, Claude
python -m src.merge
```

Search queries are configured in `config/search_queries.yaml` (shared templates plus extra per-city queries).

## Extraction: rules (default) vs Claude

The default extractor is free and deterministic:

- **emails**: regex plus `mailto:` links, with junk filtered out. The company's own domain is listed first.
- **whatsapp_number**: `wa.me` / `api.whatsapp.com` links, or a mobile number next to the word "WhatsApp". Normalised to `+91XXXXXXXXXX`.
- **gstin**: format plus the official mod-36 checksum. A state code of `08` means the business is registered in Rajasthan.
- **year_established**: "since / established / estd / founded …" followed by a year.
- **product_mix** and **mentions_jute_roll_input**: keyword dictionaries (jute fabric, hessian, laminated jute, …).
- **owner_name**: patterns like "Proprietor: Mr. X" or "X (Founder)". This is the weakest field because many sites don't name the owner.
- **size_signal**: employee counts, IndiaMART-style employee/turnover facts, exports, Pvt Ltd, certifications.
- **notes_for_sales**: a short summary built from the fields above, plus any other mobile numbers found.

`--use-llm` sends the same scraped text to `claude-sonnet-5-5` with a strict tool schema
(`EXTRACTION_SCHEMA` in `src/extract.py`). The output has the same shape, and the owner name and sales
notes are usually better. Roughly $6–10 per 300–400 sites.

## Output columns

`aariv_rajasthan_leads.csv` is sorted by `lead_score` (0–100), which is weighted toward ICP fit (mentions jute fabric
input, makes jute/canvas/promotional bags) and reachability (email, WhatsApp, owner name).
`enrichment_status` explains rows without enrichment (`no_website`, `social_link_only`, `scrape_failed`, `not_enriched`).

**Tier A** (`aariv_rajasthan_leads_tier_a.csv`) rows are fully enriched: website extracted, owner name found,
an email or WhatsApp number, and at least one product category. Change `merge.is_tier_a` if you want a looser cut.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The tests are offline: no API keys or network needed.
