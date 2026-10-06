from __future__ import annotations

from pathlib import Path

import pandas as pd

from src.dedupe import OUTPUT_COLUMNS, dedupe_by_fuzzy_name, normalize_name, run


def test_normalize_name_strips_legal_suffixes() -> None:
    assert normalize_name("Shree Jute Industries Pvt. Ltd.") == "shree jute"
    assert normalize_name("M/s Shree Jute") == "shree jute"


def test_run_dedupes_by_place_id_then_fuzzy_name(raw_gmaps_dir: Path, tmp_path: Path) -> None:
    out = tmp_path / "deduped.csv"
    df = run(raw_dir=raw_gmaps_dir, out_path=out)
    assert list(df.columns) == OUTPUT_COLUMNS
    names = sorted(df["name"])
    # A1 appears twice (place_id dedupe); A2 is the same company by fuzzy name;
    # Delhi and permanently closed places are dropped.
    assert names == ["Jute Bag Centre", "Marudhar Canvas Bags", "Marwar Jute Crafts", "Shree Jute Industries"]
    shree = df[df["name"] == "Shree Jute Industries"].iloc[0]
    assert shree["place_id"] == "A1"
    assert "Bag manufacturer" in shree["categories"] and "Wholesaler" in shree["categories"]
    assert pd.read_csv(out).shape[0] == 4


def test_run_filters_to_selected_cities(raw_gmaps_dir: Path, tmp_path: Path) -> None:
    df = run(raw_dir=raw_gmaps_dir, out_path=tmp_path / "d.csv", cities=["jodhpur"])
    assert list(df["name"]) == ["Marwar Jute Crafts"]


def test_fuzzy_keeps_similar_names_with_different_websites_apart() -> None:
    rows = [
        {"name": "Shree Ganesh Jute", "city": "Jaipur", "website": "https://ganeshjute.com"},
        {"name": "Shree Ganesh Jute.", "city": "Jaipur", "website": "https://ganesh-jute.in"},
        {"name": "Shree Ganesh Jute", "city": "Jaipur", "website": ""},
        {"name": "Shree Ganesh Jute", "city": "Jodhpur", "website": ""},
    ]
    kept = dedupe_by_fuzzy_name(rows)
    assert len(kept) == 3


def test_city_variant_is_normalized_to_searched_city(tmp_path: Path) -> None:
    import json

    raw = tmp_path / "raw"
    raw.mkdir()
    item = {"title": "Jutonomy", "city": "Jaipur, Jaipur Nagar Nigam Area", "state": "Rajasthan", "placeId": "P1"}
    payload = {"city_key": "jaipur", "city": "Jaipur", "state": "Rajasthan", "items": [item]}
    (raw / "q.json").write_text(json.dumps(payload), encoding="utf-8")
    df = run(raw_dir=raw, out_path=tmp_path / "d.csv")
    assert list(df["city"]) == ["Jaipur"]


def test_city_normalization_rules() -> None:
    from src.dedupe import _normalize_city

    known = ["Jaipur", "Ajmer", "Kishangarh", "Udaipur"]
    assert _normalize_city("Jaipur, Jaipur Nagar Nigam Area", "Jaipur", known) == "Jaipur"
    assert _normalize_city("Ajmer", "Kishangarh", known) == "Ajmer"
    assert _normalize_city("Bhuwana", "Udaipur", known) == "Udaipur"
    assert _normalize_city("", "Jaipur", known) == "Jaipur"
