"""Step 1: scrape Google Maps listings with the Apify actor compass/crawler-google-places.

One actor run per expanded search query. Raw results go to
data/raw/gmaps/<query_slug>.json. Queries whose output file already exists are
skipped, so the step can be re-run safely after an interruption.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apify_client import ApifyClient
from apify_client.errors import ApifyApiError

from src.common import (
    RAW_GMAPS_DIR,
    api_retry,
    load_config,
    require_env,
    setup_logging,
    slugify,
    write_json_atomic,
)

logger = logging.getLogger(__name__)

TERMINAL_OK_STATUSES: frozenset[str] = frozenset({"SUCCEEDED"})
# Apify rejects a max_total_charge_usd below this ("Maximum cost per run is less than the allowed minimum").
APIFY_MIN_CHARGE_CAP_USD = 0.50


@dataclass(frozen=True)
class SearchQuery:
    """One Google Maps search to run."""

    city_key: str
    city: str
    state: str
    query: str

    @property
    def slug(self) -> str:
        return slugify(self.query)


def build_queries(config: dict[str, Any], cities: list[str] | None = None) -> list[SearchQuery]:
    """Expand per-city templates from the config into concrete queries.

    `cities` is a list of city keys (e.g. ["jaipur", "jodhpur"]); None means all cities.
    """
    city_cfg: dict[str, dict[str, str]] = config["cities"]
    selected = [c.strip().lower() for c in cities] if cities else list(city_cfg)
    unknown = [c for c in selected if c not in city_cfg]
    if unknown:
        raise ValueError(f"Unknown cities {unknown}; configured: {sorted(city_cfg)}")

    extras: dict[str, list[str]] = config.get("city_extra_templates") or {}
    queries: list[SearchQuery] = []
    seen: set[str] = set()
    for key in selected:
        info = city_cfg[key]
        for template in [*config.get("templates", []), *extras.get(key, [])]:
            text = template.format(city=info["name"])
            if text.lower() in seen:
                continue
            seen.add(text.lower())
            queries.append(
                SearchQuery(city_key=key, city=info["name"], state=info.get("state", ""), query=text)
            )
    return queries


def _is_transient_apify_error(exc: BaseException) -> bool:
    if isinstance(exc, ApifyApiError):
        status = getattr(exc, "status_code", None)
        return status == 429 or (isinstance(status, int) and status >= 500)
    return isinstance(exc, (ConnectionError, TimeoutError, OSError))


def _field(obj: Any, snake: str, camel: str) -> Any:
    """Read a field from an apify-client Run (pydantic model in v3, dict in older versions)."""
    if isinstance(obj, dict):
        return obj.get(camel, obj.get(snake))
    return getattr(obj, snake, None)


def charge_cap(actor_cfg: dict[str, Any]) -> Decimal | None:
    """Per-run spending ceiling, raised to Apify's minimum if configured lower."""
    value = actor_cfg.get("max_charge_usd_per_query")
    if value is None:
        return None
    return Decimal(str(max(float(value), APIFY_MIN_CHARGE_CAP_USD)))


def build_actor_input(query: SearchQuery, actor_cfg: dict[str, Any]) -> dict[str, Any]:
    """Actor input with every paid add-on disabled so runs stay on the base per-place price."""
    return {
        "searchStringsArray": [query.query],
        # The actor's docs recommend "City, Country" over including the state.
        "locationQuery": f"{query.city}, India",
        "maxCrawledPlacesPerSearch": int(actor_cfg.get("max_places_per_query", 35)),
        "language": actor_cfg.get("language", "en"),
        "countryCode": actor_cfg.get("country_code", "in"),
        "maxReviews": 0,
        "maxImages": 0,
        "maxQuestions": 0,
        "scrapeContacts": False,
        "scrapePlaceDetailPage": False,
        "scrapeReviewsPersonalData": False,
        "skipClosedPlaces": False,
    }


def run_query(client: ApifyClient, query: SearchQuery, actor_cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Run the actor for one query and return its dataset items."""

    @api_retry(_is_transient_apify_error, logger=logger)
    def _call() -> list[dict[str, Any]]:
        run = client.actor(actor_cfg.get("id", "compass/crawler-google-places")).call(
            run_input=build_actor_input(query, actor_cfg),
            max_total_charge_usd=charge_cap(actor_cfg),
            logger=None,
        )
        if run is None:
            raise RuntimeError(f"Actor run for {query.query!r} returned no run object")
        status = str(_field(run, "status", "status")).split(".")[-1].upper()
        if status not in TERMINAL_OK_STATUSES:
            raise RuntimeError(f"Actor run for {query.query!r} ended with status {status}")
        dataset_id = _field(run, "default_dataset_id", "defaultDatasetId")
        return list(client.dataset(dataset_id).iterate_items(clean=True))

    return _call()


def output_path(query: SearchQuery, out_dir: Path = RAW_GMAPS_DIR) -> Path:
    return out_dir / f"{query.slug}.json"


def run(cities: list[str] | None = None, out_dir: Path = RAW_GMAPS_DIR) -> list[Path]:
    """Scrape every configured query for the given cities. Returns the files written this run."""
    config = load_config()
    actor_cfg: dict[str, Any] = config.get("actor", {})
    queries = build_queries(config, cities)
    out_dir.mkdir(parents=True, exist_ok=True)

    pending = [q for q in queries if not output_path(q, out_dir).exists()]
    logger.info(
        "Google Maps: %d queries configured, %d already scraped, %d to run",
        len(queries),
        len(queries) - len(pending),
        len(pending),
    )
    if not pending:
        return []

    client = ApifyClient(require_env("APIFY_API_TOKEN"))
    written: list[Path] = []
    for i, query in enumerate(pending, start=1):
        logger.info("[%d/%d] Running actor for %r", i, len(pending), query.query)
        try:
            items = run_query(client, query, actor_cfg)
        except Exception:
            logger.exception("Query %r failed; it will be retried on the next run", query.query)
            continue
        path = output_path(query, out_dir)
        write_json_atomic(
            path,
            {
                "query": query.query,
                "city_key": query.city_key,
                "city": query.city,
                "state": query.state,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "item_count": len(items),
                "items": items,
            },
        )
        logger.info("Saved %d places to %s", len(items), path.name)
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cities", help="Comma-separated city keys (default: all in config)")
    args = parser.parse_args(argv)
    setup_logging()
    run(args.cities.split(",") if args.cities else None)


if __name__ == "__main__":
    main()
