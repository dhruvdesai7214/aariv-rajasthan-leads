from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from src import dedupe, extract, merge
from tests.conftest import SAMPLE_SITE_MD


def test_end_to_end_offline(raw_gmaps_dir: Path, tmp_path: Path) -> None:
    """dedupe -> (fake scrape) -> extract -> merge without any network calls."""
    deduped = tmp_path / "companies_deduped.csv"
    dedupe.run(raw_dir=raw_gmaps_dir, out_path=deduped)

    websites = tmp_path / "websites"
    websites.mkdir()
    (websites / "shreejute.in.md").write_text(SAMPLE_SITE_MD, encoding="utf-8")
    (websites / "_failed.json").write_text(json.dumps({"marwarjute.com": "http://marwarjute.com"}), encoding="utf-8")
    enriched = tmp_path / "enriched"
    extract.run(in_dir=websites, out_dir=enriched)

    full_path, tier_a_path = merge.run(
        companies_csv=deduped, enriched_dir=enriched, websites_dir=websites, out_dir=tmp_path / "final"
    )
    full = pd.read_csv(full_path, dtype=str).fillna("")
    tier_a = pd.read_csv(tier_a_path, dtype=str).fillna("")

    assert list(full.columns) == merge.OUTPUT_COLUMNS
    assert len(full) == 4
    top = full.iloc[0]
    assert top["name"] == "Shree Jute Industries"
    assert top["enrichment_status"] == "enriched"
    assert top["owner_name"] == "Ramesh Kumar Agarwal"
    assert top["primary_email"] == "info@shreejute.in"
    assert int(top["lead_score"]) == 100

    status = dict(zip(full["name"], full["enrichment_status"]))
    assert status["Marwar Jute Crafts"] == "scrape_failed"
    assert status["Marudhar Canvas Bags"] == "no_website"
    assert status["Jute Bag Centre"] == "social_link_only"

    assert list(tier_a["name"]) == ["Shree Jute Industries"]


def test_tier_a_requires_owner_contact_and_products() -> None:
    base = {
        "enrichment_status": "enriched",
        "owner_name": "A B",
        "primary_email": "a@b.in",
        "whatsapp_number": "",
        "_product_list": ["jute bags"],
    }
    assert merge.is_tier_a(base)
    assert not merge.is_tier_a({**base, "owner_name": ""})
    assert not merge.is_tier_a({**base, "primary_email": ""})
    assert merge.is_tier_a({**base, "primary_email": "", "whatsapp_number": "+919999999999"})
    assert not merge.is_tier_a({**base, "_product_list": []})
    assert not merge.is_tier_a({**base, "enrichment_status": "not_enriched"})


def test_merge_handles_no_companies(tmp_path: Path) -> None:
    csv = tmp_path / "empty.csv"
    pd.DataFrame(columns=dedupe.OUTPUT_COLUMNS).to_csv(csv, index=False)
    enriched = tmp_path / "enriched"
    enriched.mkdir()
    full_path, tier_a_path = merge.run(
        companies_csv=csv, enriched_dir=enriched, websites_dir=tmp_path, out_dir=tmp_path / "final"
    )
    assert pd.read_csv(full_path).empty and pd.read_csv(tier_a_path).empty
