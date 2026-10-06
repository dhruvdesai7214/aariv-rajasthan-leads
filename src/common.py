"""Shared paths, logging, config loading and retry helpers for the lead pipeline."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

ROOT_DIR: Path = Path(__file__).resolve().parent.parent
CONFIG_PATH: Path = ROOT_DIR / "config" / "search_queries.yaml"
DATA_DIR: Path = ROOT_DIR / "data"
RAW_GMAPS_DIR: Path = DATA_DIR / "raw" / "gmaps"
RAW_WEBSITES_DIR: Path = DATA_DIR / "raw" / "websites"
INTERMEDIATE_DIR: Path = DATA_DIR / "intermediate"
DEDUPED_CSV: Path = INTERMEDIATE_DIR / "companies_deduped.csv"
ENRICHED_DIR: Path = DATA_DIR / "enriched"
FINAL_DIR: Path = DATA_DIR / "final"
RUNS_DIR: Path = ROOT_DIR / "runs"

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# Hosts that serve many unrelated businesses under one domain. For these the
# first path segment is part of the site key so two sellers don't collide.
SHARED_HOSTS: frozenset[str] = frozenset(
    {
        "indiamart.com",
        "m.indiamart.com",
        "dir.indiamart.com",
        "tradeindia.com",
        "exportersindia.com",
        "business.site",
        "sites.google.com",
        "justdial.com",
    }
)

# Hosts that are not company websites worth scraping.
SKIP_HOSTS: frozenset[str] = frozenset(
    {
        "facebook.com",
        "m.facebook.com",
        "instagram.com",
        "youtube.com",
        "twitter.com",
        "x.com",
        "linkedin.com",
        "in.linkedin.com",
        "wa.me",
        "api.whatsapp.com",
        "linktr.ee",
        "maps.google.com",
        "goo.gl",
        "maps.app.goo.gl",
    }
)

T = TypeVar("T")

_logging_configured = False


def setup_logging(level: int = logging.INFO) -> Path:
    """Configure root logging to runs/<timestamp>.log (and stderr) once per process.

    Returns the path of the log file in use.
    """
    global _logging_configured
    root = logging.getLogger()
    if _logging_configured:
        for handler in root.handlers:
            if isinstance(handler, logging.FileHandler):
                return Path(handler.baseFilename)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = RUNS_DIR / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    formatter = logging.Formatter(LOG_FORMAT)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    root.setLevel(level)
    root.addHandler(file_handler)
    root.addHandler(stream_handler)
    # Third-party HTTP clients are noisy at INFO.
    for noisy in ("httpx", "urllib3", "apify_client", "anthropic", "firecrawl"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _logging_configured = True
    return log_path


def load_env() -> None:
    """Load variables from the repo-level .env file (existing env vars win)."""
    load_dotenv(ROOT_DIR / ".env", override=False)


def require_env(name: str) -> str:
    """Return an environment variable or raise a clear error if it is missing."""
    load_env()
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set. Copy .env.example to .env and fill it in.")
    return value


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load the YAML search configuration."""
    with path.open(encoding="utf-8") as fh:
        config: dict[str, Any] = yaml.safe_load(fh)
    return config


def slugify(text: str) -> str:
    """Lowercase, ASCII-ish slug suitable for filenames."""
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or "untitled"


def normalize_url(url: str) -> str:
    """Add a scheme to bare website values like 'example.com'."""
    url = url.strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        url = "http://" + url
    return url


def host_of(url: str) -> str:
    """Return the lowercase host without a leading 'www.'."""
    host = urlparse(normalize_url(url)).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def site_key(url: str) -> str:
    """Stable key used for data/raw/websites/<key>.md and data/enriched/<key>.json.

    Normally this is just the domain. For shared hosts (IndiaMART, TradeIndia, ...)
    the first path segment is appended so each seller gets its own file.
    """
    if not url or not url.strip():
        return ""
    host = host_of(url)
    if not host:
        return ""
    if host in SHARED_HOSTS:
        segments = [s for s in urlparse(normalize_url(url)).path.split("/") if s]
        if segments:
            return f"{host}_{slugify(segments[0])}"
    return host


def is_scrapable_website(url: str) -> bool:
    """False for empty values and social / messaging links that aren't company sites."""
    if not url or not isinstance(url, str) or not url.strip():
        return False
    host = host_of(url)
    if not host or "." not in host:
        return False
    return not any(host == skip or host.endswith("." + skip) for skip in SKIP_HOSTS)


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON via a temp file + rename so interrupted runs never leave partial files."""
    write_text_atomic(path, json.dumps(payload, ensure_ascii=False, indent=2))


def write_text_atomic(path: Path, text: str) -> None:
    """Write text via a temp file + rename so interrupted runs never leave partial files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def api_retry(
    should_retry: Callable[[BaseException], bool],
    *,
    attempts: int = 5,
    min_wait: float = 2.0,
    max_wait: float = 60.0,
    logger: logging.Logger | None = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Tenacity retry decorator with exponential backoff for external API calls.

    `should_retry` decides which exceptions are transient. The final failure is re-raised.
    """
    log = logger or logging.getLogger(__name__)
    return retry(
        retry=retry_if_exception(should_retry),
        stop=stop_after_attempt(attempts),
        wait=wait_exponential(multiplier=min_wait, min=min_wait, max=max_wait),
        before_sleep=before_sleep_log(log, logging.WARNING),
        reraise=True,
    )
