"""Step 3: fetch company websites and save them as markdown.

For every row in companies_deduped.csv with a website, fetch the home, about and
contact pages with requests + BeautifulSoup. A page that times out or comes back
with almost no text (typically a JavaScript-rendered site) is retried through the
Firecrawl API. Output: data/raw/websites/<site_key>.md (usually the domain).

Resumable: sites that already have a .md file are skipped, and sites that failed
completely are listed in data/raw/websites/_failed.json and skipped unless
--retry-failed is passed.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup, NavigableString, Tag
from tqdm import tqdm

from src.common import (
    DEDUPED_CSV,
    RAW_WEBSITES_DIR,
    SHARED_HOSTS,
    api_retry,
    host_of,
    is_scrapable_website,
    load_env,
    normalize_url,
    setup_logging,
    site_key,
    write_json_atomic,
    write_text_atomic,
)

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_S = 15
# Below this much visible text a page is treated as empty (usually JS-rendered).
# Contact pages are legitimately short, so subpages get a lower bar.
MIN_CONTENT_CHARS = 200
MIN_SUBPAGE_CHARS = 30
MAX_HTML_BYTES = 3_000_000
FAILED_FILE = "_failed.json"
# Firecrawl's free tier allows only a few dozen requests per minute; stay well under it.
FIRECRAWL_PER_MINUTE = 12
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)

# Page path -> alternatives tried when the canonical path returns 404.
PAGE_PATHS: dict[str, list[str]] = {
    "/": [],
    "/about": ["/about-us", "/aboutus", "/about-us.html", "/about.html"],
    "/contact": ["/contact-us", "/contactus", "/contact-us.html", "/contact.html"],
}

_DROP_TAGS = ("script", "style", "noscript", "svg", "iframe", "form", "template", "head")
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside", "nav",
    "br", "tr", "table", "ul", "ol", "address", "blockquote", "figure",
}


class FetchTimeout(Exception):
    """The plain HTTP fetch timed out (triggers the Firecrawl fallback)."""


@dataclass
class PageResult:
    path: str
    url: str
    markdown: str
    method: str  # "requests" or "firecrawl"


@dataclass
class FirecrawlBudget:
    """Thread-safe cap on Firecrawl pages so a run can't drain the free credits."""

    limit: int
    used: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def take(self) -> bool:
        with self._lock:
            if self.used >= self.limit:
                return False
            self.used += 1
            return True


# --------------------------------------------------------------------------- #
# HTML -> markdown
# --------------------------------------------------------------------------- #


def _collect_contact_links(soup: BeautifulSoup, base_url: str) -> list[str]:
    """mailto:, tel: and WhatsApp links are the highest-value bits for outreach; keep them verbatim."""
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        href = str(a["href"]).strip()
        low = href.lower()
        if low.startswith(("mailto:", "tel:")) or "wa.me/" in low or "whatsapp.com/send" in low:
            found.append(href)
        elif low.startswith("/") and ("wa.me" in low or "whatsapp" in low):
            found.append(urljoin(base_url, href))
    return list(dict.fromkeys(found))


def _render(node: Tag | NavigableString, out: list[str]) -> None:
    if isinstance(node, NavigableString):
        out.append(re.sub(r"\s+", " ", str(node)))
        return
    name = node.name or ""
    if name in _DROP_TAGS:
        return
    if re.fullmatch(r"h[1-6]", name):
        text = node.get_text(" ", strip=True)
        if text:
            out.append(f"\n\n{'#' * int(name[1])} {text}\n\n")
        return
    if name == "li":
        text = node.get_text(" ", strip=True)
        if text:
            out.append(f"\n- {text}")
        return
    if name in ("td", "th"):
        out.append(" | ")
    if name in _BLOCK_TAGS:
        out.append("\n")
    for child in node.children:
        _render(child, out)  # type: ignore[arg-type]
    if name in _BLOCK_TAGS:
        out.append("\n")


def html_to_markdown(html: str, base_url: str = "") -> str:
    """Convert HTML to compact markdown-ish text (headings, lists, paragraphs, contact links)."""
    soup = BeautifulSoup(html, "html.parser")
    contact_links = _collect_contact_links(soup, base_url)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta_desc = ""
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if isinstance(meta, Tag) and meta.get("content"):
        meta_desc = str(meta["content"]).strip()

    for tag in soup(list(_DROP_TAGS)):
        tag.decompose()
    body = soup.body or soup
    parts: list[str] = []
    _render(body, parts)
    text = "".join(parts)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()

    header: list[str] = []
    if title:
        header.append(f"Title: {title}")
    if meta_desc:
        header.append(f"Description: {meta_desc}")
    if contact_links:
        header.append("Contact links: " + " ".join(contact_links))
    return ("\n".join(header) + "\n\n" + text).strip() if header else text


_PARKED_PAGE = re.compile(
    r"parked domain|domain (?:name )?(?:is )?for sale|buy this domain|if this is your domain|"
    r"this domain (?:has expired|is parked)|account (?:has been )?suspended",
    re.I,
)


def is_parked_page(markdown: str) -> bool:
    """Registrar parking / expired / suspended pages: there's no company content to extract."""
    return _PARKED_PAGE.search(markdown[:5000]) is not None


def visible_text_length(markdown: str) -> int:
    """Length of the page text excluding the Title/Description/Contact links header lines."""
    body = re.sub(r"^(Title|Description|Contact links): .*$", "", markdown, flags=re.M)
    return len(re.sub(r"\s+", "", body))


# --------------------------------------------------------------------------- #
# Fetching
# --------------------------------------------------------------------------- #


def _is_transient_http_error(exc: BaseException) -> bool:
    return isinstance(exc, requests.ConnectionError) and not isinstance(exc, requests.Timeout)


@api_retry(_is_transient_http_error, attempts=3, min_wait=1.0, max_wait=8.0, logger=logger)
def fetch_html(session: requests.Session, url: str) -> tuple[int, str, str]:
    """GET a page. Returns (status_code, final_url, html). Raises FetchTimeout on timeout."""
    try:
        with session.get(url, timeout=REQUEST_TIMEOUT_S, allow_redirects=True, stream=True) as resp:
            raw = resp.raw.read(MAX_HTML_BYTES, decode_content=True)
            status, final_url = resp.status_code, resp.url
            ctype = resp.headers.get("Content-Type", "")
    except requests.Timeout as exc:
        raise FetchTimeout(url) from exc
    except requests.exceptions.SSLError:
        # Many small business sites have broken certificates; fall back to plain http once.
        if url.startswith("https://"):
            return fetch_html(session, "http://" + url[len("https://"):])
        raise
    if ctype and "html" not in ctype and "text" not in ctype:
        return status, final_url, ""
    charset = re.search(r"charset=([\w-]+)", ctype, flags=re.I)
    try:
        html = raw.decode(charset.group(1) if charset else "utf-8", errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")
    return status, final_url, html


def _is_transient_firecrawl_error(exc: BaseException) -> bool:
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return True
    return type(exc).__name__ in {"RateLimitError", "InternalServerError", "RequestTimeoutError"}


class RateLimiter:
    """Spaces calls at least `60 / per_minute` seconds apart across threads."""

    def __init__(self, per_minute: float) -> None:
        self.interval = 60.0 / per_minute if per_minute > 0 else 0.0
        self._next_at = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next_at - now
            self._next_at = max(now, self._next_at) + self.interval
        if delay > 0:
            time.sleep(delay)


class FirecrawlFetcher:
    """Thin wrapper around firecrawl-py with retries, a page budget and client-side rate limiting."""

    def __init__(self, api_key: str, budget: FirecrawlBudget, per_minute: float = FIRECRAWL_PER_MINUTE) -> None:
        from firecrawl import Firecrawl

        self._client = Firecrawl(api_key=api_key)
        self.budget = budget
        self._limiter = RateLimiter(per_minute)

    def scrape(self, url: str) -> str:
        if not self.budget.take():
            logger.warning("Firecrawl page budget (%d) used up; skipping %s", self.budget.limit, url)
            return ""

        # Rate-limit windows are a minute long, so back off 15s -> 30s -> 60s rather than seconds.
        @api_retry(_is_transient_firecrawl_error, attempts=4, min_wait=15.0, max_wait=60.0, logger=logger)
        def _call() -> str:
            self._limiter.wait()
            doc: Any = self._client.scrape(url, formats=["markdown"], only_main_content=False, timeout=45_000)
            markdown = getattr(doc, "markdown", None)
            if markdown is None and isinstance(doc, dict):
                markdown = doc.get("markdown")
            return str(markdown or "")

        try:
            return _call()
        except Exception:
            logger.exception("Firecrawl failed for %s", url)
            return ""


def _candidate_urls(website: str, path: str) -> list[str]:
    url = normalize_url(website)
    parsed = urlparse(url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    return [urljoin(base, p) for p in [path, *PAGE_PATHS[path]]]


def fetch_page(
    session: requests.Session,
    website: str,
    path: str,
    firecrawl: FirecrawlFetcher | None,
) -> PageResult | None:
    """Fetch one logical page (/, /about, /contact), falling back to Firecrawl when needed."""
    fallback_url: str | None = None
    for url in _candidate_urls(website, path):
        try:
            status, final_url, html = fetch_html(session, url)
        except FetchTimeout:
            logger.info("Timeout on %s", url)
            fallback_url = url
            break
        except requests.RequestException as exc:
            logger.info("Request failed for %s: %s", url, exc)
            if path == "/":
                fallback_url = url
            break
        if status == 404 or status == 410:
            continue  # page doesn't exist; try the next alternative path
        if status >= 400:
            logger.info("HTTP %d on %s", status, url)
            fallback_url = url  # 403/5xx often means bot protection; Firecrawl may get through
            break
        markdown = html_to_markdown(html, final_url)
        if is_parked_page(markdown):
            logger.info("Parked/expired domain page at %s", final_url)
            return None
        min_chars = MIN_CONTENT_CHARS if path == "/" else MIN_SUBPAGE_CHARS
        if visible_text_length(markdown) >= min_chars:
            return PageResult(path=path, url=final_url, markdown=markdown, method="requests")
        logger.info("Almost no text on %s (likely JS-rendered)", final_url)
        fallback_url = final_url
        break

    if fallback_url and firecrawl is not None:
        markdown = firecrawl.scrape(fallback_url)
        if markdown.strip() and not is_parked_page(markdown):
            return PageResult(path=path, url=fallback_url, markdown=markdown.strip(), method="firecrawl")
    return None


def scrape_site(
    website: str,
    firecrawl: FirecrawlFetcher | None,
    session: requests.Session | None = None,
) -> list[PageResult]:
    """Fetch home/about/contact for one site. Shared hosts (IndiaMART etc.) only fetch the given URL."""
    own_session = session is None
    session = session or _new_session()
    try:
        if host_of(website) in SHARED_HOSTS:
            url = normalize_url(website)
            try:
                status, final_url, html = fetch_html(session, url)
                md = html_to_markdown(html, final_url) if status < 400 else ""
            except (FetchTimeout, requests.RequestException):
                md, final_url = "", url
            if visible_text_length(md) >= MIN_CONTENT_CHARS:
                return [PageResult(path="/", url=final_url, markdown=md, method="requests")]
            if firecrawl is not None:
                md = firecrawl.scrape(url)
                if md.strip():
                    return [PageResult(path="/", url=url, markdown=md.strip(), method="firecrawl")]
            return []

        pages: list[PageResult] = []
        seen_urls: set[str] = set()
        subpage_firecrawl = firecrawl
        for path in PAGE_PATHS:
            page = fetch_page(session, website, path, firecrawl if path == "/" else subpage_firecrawl)
            if page is None:
                if path == "/":
                    break  # home page unreachable even via fallback: the site is down or blocking us
                continue
            if path == "/" and page.method == "firecrawl":
                # The site blocks plain requests, so its subpages will too. The home page usually
                # carries the contact footer; don't spend two more Firecrawl credits per blocked site.
                subpage_firecrawl = None
            # /about often redirects to /; don't save the same page twice.
            if page.url in seen_urls:
                continue
            seen_urls.add(page.url)
            pages.append(page)
        return pages
    finally:
        if own_session:
            session.close()


def render_site_markdown(key: str, website: str, pages: list[PageResult]) -> str:
    """Combine all pages for one site into a single markdown document."""
    lines = [
        f"# {key}",
        "",
        f"website: {website}",
        f"scraped_at: {datetime.now(timezone.utc).isoformat()}",
        "",
    ]
    for page in pages:
        lines += [f"## Page {page.path} — {page.url} (via {page.method})", "", page.markdown, ""]
    return "\n".join(lines).rstrip() + "\n"


def _new_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        }
    )
    return session


# --------------------------------------------------------------------------- #
# Step runner
# --------------------------------------------------------------------------- #


def collect_sites(csv_path: Path = DEDUPED_CSV) -> dict[str, str]:
    """Map site_key -> website URL for every scrapable website in the deduped CSV."""
    df = pd.read_csv(csv_path, dtype=str).fillna("")
    sites: dict[str, str] = {}
    skipped = 0
    for website in df["website"]:
        if not is_scrapable_website(website):
            if website.strip():
                skipped += 1
            continue
        key = site_key(website)
        if key and key not in sites:
            sites[key] = website.strip()
    logger.info("%d unique websites to consider (%d social/messaging links skipped)", len(sites), skipped)
    return sites


def _load_failed(out_dir: Path) -> dict[str, str]:
    path = out_dir / FAILED_FILE
    if not path.exists():
        return {}
    try:
        data: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
        return data
    except json.JSONDecodeError:
        return {}


def run(
    csv_path: Path = DEDUPED_CSV,
    out_dir: Path = RAW_WEBSITES_DIR,
    workers: int = 8,
    max_firecrawl_pages: int = 150,
    retry_failed: bool = False,
) -> list[Path]:
    """Scrape every pending website. Returns the markdown files written this run."""
    out_dir.mkdir(parents=True, exist_ok=True)
    sites = collect_sites(csv_path)
    failed = {} if retry_failed else _load_failed(out_dir)
    pending = {
        k: url for k, url in sites.items() if not (out_dir / f"{k}.md").exists() and k not in failed
    }
    logger.info(
        "Websites: %d total, %d already scraped, %d previously failed, %d to fetch",
        len(sites),
        sum(1 for k in sites if (out_dir / f"{k}.md").exists()),
        sum(1 for k in sites if k in failed),
        len(pending),
    )
    if not pending:
        return []

    load_env()
    fc_key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    firecrawl = FirecrawlFetcher(fc_key, FirecrawlBudget(max_firecrawl_pages)) if fc_key else None
    if firecrawl is None:
        logger.warning("FIRECRAWL_API_KEY not set; JS-heavy or slow sites will not be retried via Firecrawl")

    written: list[Path] = []
    lock = threading.Lock()

    def _work(key: str, website: str) -> None:
        pages = scrape_site(website, firecrawl)
        with lock:
            if pages:
                path = out_dir / f"{key}.md"
                write_text_atomic(path, render_site_markdown(key, website, pages))
                written.append(path)
                failed.pop(key, None)
            else:
                failed[key] = website
                logger.info("No usable content for %s", website)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_work, k, url): k for k, url in pending.items()}
        for fut in tqdm(as_completed(futures), total=len(futures), desc="websites", unit="site"):
            try:
                fut.result()
            except Exception:
                logger.exception("Unexpected error scraping %s", futures[fut])

    write_json_atomic(out_dir / FAILED_FILE, dict(sorted(failed.items())))
    logger.info(
        "Saved %d sites; %d failed; Firecrawl pages used: %d",
        len(written),
        len(failed),
        firecrawl.budget.used if firecrawl else 0,
    )
    return written


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workers", type=int, default=8, help="Parallel site fetches (default 8)")
    parser.add_argument(
        "--max-firecrawl-pages",
        type=int,
        default=150,
        help="Upper bound on Firecrawl fallback pages this run, to protect free credits (default 150)",
    )
    parser.add_argument("--retry-failed", action="store_true", help="Retry sites listed in _failed.json")
    args = parser.parse_args(argv)
    setup_logging()
    run(workers=args.workers, max_firecrawl_pages=args.max_firecrawl_pages, retry_failed=args.retry_failed)


if __name__ == "__main__":
    main()
