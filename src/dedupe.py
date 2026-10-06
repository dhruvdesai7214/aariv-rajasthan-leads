"""Step 2: merge all raw Google Maps results into one deduplicated company list.

Dedupe order:
  1. exact place_id
  2. fuzzy company-name match (rapidfuzz token_sort_ratio >= 85) within the same city

Output: data/intermediate/companies_deduped.csv
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any

import pandas as pd
from rapidfuzz import fuzz, process

from src.common import DEDUPED_CSV, RAW_GMAPS_DIR, setup_logging, site_key, write_text_atomic

logger = logging.getLogger(__name__)

FUZZY_THRESHOLD = 85

OUTPUT_COLUMNS: list[str] = [
    "name",
    "address",
    "city",
    "state",
    "phone",
    "website",
    "gmaps_rating",
    "review_count",
    "categories",
    "gmaps_url",
    "place_id",
]

# Legal / filler words that shouldn't make two different names look alike or
# make the same company look different.
_NAME_NOISE = re.compile(
    r"\b(pvt|private|ltd|limited|llp|co|company|corp|corporation|inc|and|the|"
    r"m/s|ms|enterprises?|industries|industry|international|intl|india|jaipur|jodhpur|"
    r"kishangarh|udaipur|ajmer|bikaner|rajasthan)\b"
)


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation and legal suffixes for fuzzy comparison."""
    text = re.sub(r"[^a-z0-9/ ]+", " ", (name or "").lower())
    text = _NAME_NOISE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    return value


def item_to_row(
    item: dict[str, Any], default_city: str, default_state: str, city_key: str = ""
) -> dict[str, Any]:
    """Map one compass/crawler-google-places item to our flat row schema."""
    categories = item.get("categories") or ([item["categoryName"]] if item.get("categoryName") else [])
    return {
        "name": _clean(item.get("title")),
        "address": _clean(item.get("address")),
        "city": _clean(item.get("city")) or default_city,
        "state": _clean(item.get("state")) or default_state,
        "phone": _clean(item.get("phone") or item.get("phoneUnformatted")),
        "website": _clean(item.get("website")),
        "gmaps_rating": _clean(item.get("totalScore")),
        "review_count": _clean(item.get("reviewsCount")),
        "categories": "; ".join(str(c) for c in categories if c),
        "gmaps_url": _clean(item.get("url")),
        "place_id": _clean(item.get("placeId")),
        "_permanently_closed": bool(item.get("permanentlyClosed")),
        "_city_key": city_key or default_city.lower(),
    }


def load_raw_rows(raw_dir: Path = RAW_GMAPS_DIR) -> list[dict[str, Any]]:
    """Load every data/raw/gmaps/*.json file into flat rows."""
    rows: list[dict[str, Any]] = []
    files = sorted(raw_dir.glob("*.json"))
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.exception("Skipping unreadable raw file %s", path)
            continue
        # Accept both our wrapper object and a bare list of actor items.
        if isinstance(payload, list):
            items, city, state, city_key = payload, "", "", ""
        else:
            items = payload.get("items", [])
            city, state = payload.get("city", ""), payload.get("state", "")
            city_key = payload.get("city_key", "")
        rows.extend(item_to_row(item, city, state, city_key) for item in items if item.get("title"))
    logger.info("Loaded %d raw places from %d files", len(rows), len(files))
    return rows


def _richness(row: dict[str, Any]) -> int:
    """How many output fields are filled; used to pick which duplicate to keep."""
    return sum(1 for col in OUTPUT_COLUMNS if str(row.get(col, "")).strip())


def _merge_into(keep: dict[str, Any], other: dict[str, Any]) -> None:
    """Fill gaps in `keep` from `other` and union their categories."""
    for col in OUTPUT_COLUMNS:
        if not str(keep.get(col, "")).strip() and str(other.get(col, "")).strip():
            keep[col] = other[col]
    cats = [c.strip() for c in f"{keep.get('categories', '')};{other.get('categories', '')}".split(";")]
    keep["categories"] = "; ".join(dict.fromkeys(c for c in cats if c))


def dedupe_by_place_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    no_id: list[dict[str, Any]] = []
    for row in sorted(rows, key=_richness, reverse=True):
        pid = str(row.get("place_id", "")).strip()
        if not pid:
            no_id.append(dict(row))
        elif pid in by_id:
            _merge_into(by_id[pid], row)
        else:
            by_id[pid] = dict(row)
    return [*by_id.values(), *no_id]


def _websites_conflict(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """True when both rows have websites that point at different sites (so they're different companies)."""
    ka, kb = site_key(str(a.get("website", ""))), site_key(str(b.get("website", "")))
    return bool(ka and kb and ka != kb)


def dedupe_by_fuzzy_name(rows: list[dict[str, Any]], threshold: int = FUZZY_THRESHOLD) -> list[dict[str, Any]]:
    """Collapse rows whose normalized names match >= threshold within the same city.

    Two similar names are kept apart when their websites point at different sites.
    """
    kept: list[dict[str, Any]] = []
    # Per-city lists of (normalized name, index into kept).
    by_city: dict[str, list[tuple[str, int]]] = {}
    for row in sorted(rows, key=_richness, reverse=True):
        city = str(row.get("city", "")).strip().lower()
        norm = normalize_name(str(row.get("name", ""))) or str(row.get("name", "")).lower()
        candidates = by_city.setdefault(city, [])
        target: dict[str, Any] | None = None
        if candidates:
            matches = process.extract(
                norm,
                [c[0] for c in candidates],
                scorer=fuzz.token_sort_ratio,
                score_cutoff=threshold,
                limit=5,
            )
            for _, _, idx in matches:
                existing = kept[candidates[idx][1]]
                if not _websites_conflict(existing, row):
                    target = existing
                    break
        if target is not None:
            _merge_into(target, row)
        else:
            kept.append(dict(row))
            candidates.append((norm, len(kept) - 1))
    return kept


def filter_rows(rows: list[dict[str, Any]], cities: list[str] | None = None) -> list[dict[str, Any]]:
    """Drop permanently closed places, places outside Rajasthan and unselected cities."""
    selected = {c.strip().lower() for c in cities} if cities else None
    out: list[dict[str, Any]] = []
    dropped_closed = dropped_state = dropped_city = 0
    for row in rows:
        if row.get("_permanently_closed"):
            dropped_closed += 1
            continue
        state = str(row.get("state", "")).strip().lower()
        address = str(row.get("address", "")).lower()
        if state and state != "rajasthan" and "rajasthan" not in address:
            dropped_state += 1
            continue
        if selected is not None and str(row.get("_city_key", "")).strip().lower() not in selected:
            dropped_city += 1
            continue
        out.append(row)
    logger.info(
        "Filtered out %d closed, %d outside Rajasthan, %d from unselected cities",
        dropped_closed,
        dropped_state,
        dropped_city,
    )
    return out


def run(
    raw_dir: Path = RAW_GMAPS_DIR,
    out_path: Path = DEDUPED_CSV,
    cities: list[str] | None = None,
) -> pd.DataFrame:
    """Load, filter, dedupe and write companies_deduped.csv. Returns the written frame."""
    rows = filter_rows(load_raw_rows(raw_dir), cities)
    by_id = dedupe_by_place_id(rows)
    logger.info("place_id dedupe: %d -> %d", len(rows), len(by_id))
    final = dedupe_by_fuzzy_name(by_id)
    logger.info("Fuzzy name dedupe (>= %d): %d -> %d", FUZZY_THRESHOLD, len(by_id), len(final))

    df = pd.DataFrame(final, columns=OUTPUT_COLUMNS).sort_values(["city", "name"], kind="stable")
    write_text_atomic(out_path, df.to_csv(index=False))
    logger.info("Wrote %d companies to %s", len(df), out_path)
    return df


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cities", help="Comma-separated city keys to keep (default: all)")
    args = parser.parse_args(argv)
    setup_logging()
    run(cities=args.cities.split(",") if args.cities else None)


if __name__ == "__main__":
    main()
