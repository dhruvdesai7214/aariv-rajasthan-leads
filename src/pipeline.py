"""Run the whole Aariv Fabrics Rajasthan lead pipeline in order.

    python -m src.pipeline --cities jaipur,jodhpur,kishangarh

Steps: gmaps -> dedupe -> websites -> extract -> merge. Every step is resumable,
so re-running the same command picks up where an interrupted run stopped.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable

from src import dedupe, extract, merge, scrape_gmaps, scrape_websites
from src.common import load_config, setup_logging

logger = logging.getLogger("src.pipeline")

STEPS: list[str] = ["gmaps", "dedupe", "websites", "extract", "merge"]


def parse_cities(value: str | None) -> list[str] | None:
    """Parse and validate a comma-separated city list against the config."""
    if not value:
        return None
    cities = [c.strip().lower() for c in value.split(",") if c.strip()]
    configured = set(load_config()["cities"])
    unknown = [c for c in cities if c not in configured]
    if unknown:
        raise argparse.ArgumentTypeError(f"Unknown cities {unknown}; choose from {sorted(configured)}")
    return cities


def parse_steps(value: str | None) -> list[str]:
    if not value:
        return list(STEPS)
    steps = [s.strip().lower() for s in value.split(",") if s.strip()]
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        raise argparse.ArgumentTypeError(f"Unknown steps {unknown}; choose from {STEPS}")
    return [s for s in STEPS if s in steps]


def run(
    cities: list[str] | None = None,
    steps: list[str] | None = None,
    use_llm: bool = False,
    workers: int = 8,
    max_firecrawl_pages: int = 150,
) -> None:
    steps = steps or list(STEPS)
    actions: dict[str, Callable[[], object]] = {
        "gmaps": lambda: scrape_gmaps.run(cities),
        "dedupe": lambda: dedupe.run(cities=cities),
        "websites": lambda: scrape_websites.run(workers=workers, max_firecrawl_pages=max_firecrawl_pages),
        "extract": lambda: extract.run(use_llm=use_llm),
        "merge": lambda: merge.run(),
    }
    logger.info("Pipeline start: cities=%s steps=%s extractor=%s", cities or "all", steps, "llm" if use_llm else "rules")
    for step in steps:
        started = time.monotonic()
        logger.info("=== Step: %s ===", step)
        actions[step]()
        logger.info("=== Step %s finished in %.1fs ===", step, time.monotonic() - started)
    logger.info("Pipeline complete")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cities", type=parse_cities, help="Comma-separated city keys (default: all in config)")
    parser.add_argument("--steps", type=parse_steps, help=f"Comma-separated subset of {','.join(STEPS)}")
    parser.add_argument("--use-llm", action="store_true", help="Use Claude for extraction (paid) instead of rules")
    parser.add_argument("--workers", type=int, default=8, help="Parallel website fetches (default 8)")
    parser.add_argument("--max-firecrawl-pages", type=int, default=150, help="Firecrawl fallback page cap per run")
    args = parser.parse_args(argv)
    log_path = setup_logging()
    logger.info("Logging to %s", log_path)
    try:
        run(
            cities=args.cities,
            steps=args.steps,
            use_llm=args.use_llm,
            workers=args.workers,
            max_firecrawl_pages=args.max_firecrawl_pages,
        )
    except RuntimeError as exc:
        # Configuration problems (e.g. a missing API key): report cleanly, no traceback.
        logger.error("Pipeline stopped: %s", exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
