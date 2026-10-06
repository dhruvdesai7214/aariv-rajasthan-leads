"""Step 5: join the deduped company list with website enrichment into sales-ready CSVs.

Outputs:
  data/final/aariv_rajasthan_leads.csv         every company, best leads first
  data/final/aariv_rajasthan_leads_tier_a.csv  fully enriched rows only (see is_tier_a)
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from src.common import (
    DEDUPED_CSV,
    ENRICHED_DIR,
    FINAL_DIR,
    RAW_WEBSITES_DIR,
    is_scrapable_website,
    setup_logging,
    site_key,
    write_text_atomic,
)

logger = logging.getLogger(__name__)

FULL_CSV_NAME = "aariv_rajasthan_leads.csv"
TIER_A_CSV_NAME = "aariv_rajasthan_leads_tier_a.csv"

ENRICHED_FIELDS: list[str] = [
    "owner_name",
    "emails",
    "whatsapp_number",
    "gstin",
    "year_established",
    "product_mix",
    "mentions_jute_roll_input",
    "size_signal",
    "notes_for_sales",
]

OUTPUT_COLUMNS: list[str] = [
    "lead_score",
    "name",
    "city",
    "owner_name",
    "phone",
    "whatsapp_number",
    "primary_email",
    "emails",
    "website",
    "product_mix",
    "mentions_jute_roll_input",
    "size_signal",
    "notes_for_sales",
    "gstin",
    "year_established",
    "gmaps_rating",
    "review_count",
    "categories",
    "address",
    "state",
    "gmaps_url",
    "place_id",
    "site_key",
    "enrichment_status",
    "extraction_method",
]

ICP_PRODUCTS = {"jute bags", "juco bags", "canvas bags", "cotton bags", "promotional bags", "laminated jute"}


def load_enriched(enriched_dir: Path = ENRICHED_DIR) -> dict[str, dict[str, Any]]:
    """Map site_key -> enrichment record."""
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(enriched_dir.glob("*.json")):
        try:
            records[path.stem] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.exception("Skipping unreadable enrichment file %s", path)
    logger.info("Loaded %d enrichment records", len(records))
    return records


def _failed_sites(websites_dir: Path) -> set[str]:
    path = websites_dir / "_failed.json"
    if not path.exists():
        return set()
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except json.JSONDecodeError:
        return set()


def lead_score(row: dict[str, Any]) -> int:
    """0-100 priority for Aariv's cold outreach: ICP fit first, then how reachable the lead is."""
    score = 0
    products = set(row.get("_product_list") or [])
    if row.get("mentions_jute_roll_input") is True:
        score += 30
    if products & ICP_PRODUCTS:
        score += 20
    elif "jute" in str(row.get("categories", "")).lower() or "bag" in str(row.get("categories", "")).lower():
        score += 10
    if row.get("primary_email"):
        score += 15
    if row.get("whatsapp_number"):
        score += 15
    elif row.get("phone"):
        score += 5
    if row.get("owner_name"):
        score += 10
    if row.get("size_signal") in ("small", "mid"):
        score += 10
    return min(score, 100)


def is_tier_a(row: dict[str, Any]) -> bool:
    """Fully enriched: website extracted, a named owner, a direct channel (email or WhatsApp),
    and at least one product category identified."""
    return (
        row.get("enrichment_status") == "enriched"
        and bool(row.get("owner_name"))
        and bool(row.get("primary_email") or row.get("whatsapp_number"))
        and bool(row.get("_product_list"))
    )


def merge_rows(
    companies: pd.DataFrame,
    enriched: dict[str, dict[str, Any]],
    failed_sites: set[str] | None = None,
) -> pd.DataFrame:
    failed_sites = failed_sites or set()
    rows: list[dict[str, Any]] = []
    for company in companies.to_dict(orient="records"):
        website = str(company.get("website") or "")
        key = site_key(website) if is_scrapable_website(website) else ""
        record = enriched.get(key) if key else None
        row: dict[str, Any] = dict(company)
        row["site_key"] = key
        if record is not None:
            status = "enriched"
        elif not website:
            status = "no_website"
        elif not key:
            status = "social_link_only"
        elif key in failed_sites:
            status = "scrape_failed"
        else:
            status = "not_enriched"
        row["enrichment_status"] = status
        record = record or {}
        emails: list[str] = list(record.get("emails") or [])
        products: list[str] = list(record.get("product_mix") or [])
        row.update(
            {
                "owner_name": record.get("owner_name") or "",
                "emails": "; ".join(emails),
                "primary_email": emails[0] if emails else "",
                "whatsapp_number": record.get("whatsapp_number") or "",
                "gstin": record.get("gstin") or "",
                "year_established": record.get("year_established") or "",
                "product_mix": "; ".join(products),
                "_product_list": products,
                "mentions_jute_roll_input": record.get("mentions_jute_roll_input") if record else "",
                "size_signal": record.get("size_signal") or "",
                "notes_for_sales": record.get("notes_for_sales") or "",
                "extraction_method": record.get("extraction_method") or "",
            }
        )
        row["lead_score"] = lead_score(row)
        row["_tier_a"] = is_tier_a(row)
        rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return pd.DataFrame(columns=[*OUTPUT_COLUMNS, "_tier_a"])
    df = df.sort_values(["lead_score", "review_count"], ascending=[False, False], kind="stable", na_position="last")
    return df[[*OUTPUT_COLUMNS, "_tier_a"]]


def run(
    companies_csv: Path = DEDUPED_CSV,
    enriched_dir: Path = ENRICHED_DIR,
    websites_dir: Path = RAW_WEBSITES_DIR,
    out_dir: Path = FINAL_DIR,
) -> tuple[Path, Path]:
    """Write the full and Tier A CSVs. Returns their paths."""
    companies = pd.read_csv(companies_csv, dtype={"place_id": str, "phone": str}).fillna("")
    companies["review_count"] = pd.to_numeric(companies["review_count"], errors="coerce").fillna(0).astype(int)
    merged = merge_rows(companies, load_enriched(enriched_dir), _failed_sites(websites_dir))

    full_path = out_dir / FULL_CSV_NAME
    tier_a_path = out_dir / TIER_A_CSV_NAME
    tier_a = merged[merged["_tier_a"].astype(bool)]
    write_text_atomic(full_path, merged.drop(columns="_tier_a").to_csv(index=False))
    write_text_atomic(tier_a_path, tier_a.drop(columns="_tier_a").to_csv(index=False))

    status_counts = merged["enrichment_status"].value_counts().to_dict() if not merged.empty else {}
    logger.info("Enrichment status: %s", status_counts)
    logger.info("Wrote %d leads to %s and %d Tier A leads to %s", len(merged), full_path, len(tier_a), tier_a_path)
    return full_path, tier_a_path


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args(argv)
    setup_logging()
    run()


if __name__ == "__main__":
    main()
