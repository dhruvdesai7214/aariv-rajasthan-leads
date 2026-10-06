from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src import scrape_websites as sw
from src.common import is_scrapable_website, site_key

HOME_HTML = """<html><head><title>Shree Jute</title>
<meta name="description" content="Jute bag maker"><script>var x=1;</script></head>
<body><nav><ul><li>Home</li><li>About</li></ul></nav>
<h1>Jute Bags Manufacturer</h1><p>We make jute shopping bags from laminated jute fabric in Jaipur.
We have been doing this for many years and serve customers across India and abroad.
Our range covers jute wine bags, juco tote bags and printed promotional bags for corporate events.</p>
<a href="mailto:info@shreejute.in">Mail</a> <a href="https://wa.me/919829012345">Chat</a>
<table><tr><td>GST</td><td>08AAACR5055K1Z3</td></tr></table></body></html>"""


def test_html_to_markdown_keeps_structure_and_contact_links() -> None:
    md = sw.html_to_markdown(HOME_HTML, "https://shreejute.in/")
    assert "Title: Shree Jute" in md
    assert "Contact links: mailto:info@shreejute.in https://wa.me/919829012345" in md
    assert "# Jute Bags Manufacturer" in md
    assert "- Home" in md
    assert "var x" not in md
    assert "Mail Chat" in md
    assert sw.visible_text_length(md) > sw.MIN_CONTENT_CHARS


def test_site_key_and_scrapable() -> None:
    assert site_key("https://www.ShreeJute.in/about") == "shreejute.in"
    assert site_key("shreejute.in") == "shreejute.in"
    assert site_key("https://www.indiamart.com/shree-jute/") == "indiamart.com_shree_jute"
    assert not is_scrapable_website("https://www.facebook.com/shreejute")
    assert not is_scrapable_website("")
    assert is_scrapable_website("http://marwarjute.com")


class FakeFirecrawl:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.budget = sw.FirecrawlBudget(10)

    def scrape(self, url: str) -> str:
        self.calls.append(url)
        return "# Rendered by Firecrawl\nJute bags and hessian rolls."


def test_fetch_page_falls_back_to_firecrawl_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_fetch(session: object, url: str) -> tuple[int, str, str]:
        raise sw.FetchTimeout(url)

    monkeypatch.setattr(sw, "fetch_html", fake_fetch)
    fc = FakeFirecrawl()
    page = sw.fetch_page(object(), "https://slow.example.in", "/", fc)  # type: ignore[arg-type]
    assert page is not None and page.method == "firecrawl"
    assert fc.calls == ["https://slow.example.in/"]


def test_short_contact_page_does_not_use_firecrawl(monkeypatch: pytest.MonkeyPatch) -> None:
    html = "<html><body><p>Call us: +91 94140 11111, Sitapura, Jaipur</p></body></html>"
    monkeypatch.setattr(sw, "fetch_html", lambda s, u: (200, u, html))
    fc = FakeFirecrawl()
    page = sw.fetch_page(object(), "https://shreejute.in", "/contact", fc)  # type: ignore[arg-type]
    assert page is not None and page.method == "requests"
    assert fc.calls == []


def test_fetch_page_falls_back_on_empty_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sw, "fetch_html", lambda s, u: (200, u, "<html><body><div id=root></div></body></html>"))
    fc = FakeFirecrawl()
    page = sw.fetch_page(object(), "https://spa.example.in", "/", fc)  # type: ignore[arg-type]
    assert page is not None and page.method == "firecrawl"


def test_fetch_page_tries_alternative_paths_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake_fetch(session: object, url: str) -> tuple[int, str, str]:
        seen.append(url)
        if url.endswith("/about-us"):
            return 200, url, HOME_HTML
        return 404, url, ""

    monkeypatch.setattr(sw, "fetch_html", fake_fetch)
    fc = FakeFirecrawl()
    page = sw.fetch_page(object(), "https://shreejute.in", "/about", fc)  # type: ignore[arg-type]
    assert page is not None and page.method == "requests" and page.url.endswith("/about-us")
    assert seen == ["https://shreejute.in/about", "https://shreejute.in/about-us"]
    assert fc.calls == []  # a missing page is not a reason to spend Firecrawl credits


def test_firecrawl_budget_caps_usage() -> None:
    budget = sw.FirecrawlBudget(2)
    assert [budget.take() for _ in range(3)] == [True, True, False]


def test_run_is_resumable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    csv = tmp_path / "companies.csv"
    pd.DataFrame(
        {"website": ["https://done.in", "https://new.in", "https://broken.in", "https://facebook.com/x", ""]}
    ).to_csv(csv, index=False)
    out = tmp_path / "websites"
    out.mkdir()
    (out / "done.in.md").write_text("existing", encoding="utf-8")
    scraped: list[str] = []

    def fake_scrape_site(website: str, firecrawl: object, session: object = None) -> list[sw.PageResult]:
        scraped.append(website)
        if "broken" in website:
            return []
        return [sw.PageResult("/", website, "content", "requests")]

    monkeypatch.setattr(sw, "scrape_site", fake_scrape_site)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(sw, "load_env", lambda: None)
    written = sw.run(csv_path=csv, out_dir=out, workers=2)
    assert sorted(scraped) == ["https://broken.in", "https://new.in"]
    assert [p.name for p in written] == ["new.in.md"]
    assert "broken.in" in (out / sw.FAILED_FILE).read_text(encoding="utf-8")

    scraped.clear()
    assert sw.run(csv_path=csv, out_dir=out, workers=2) == []
    assert scraped == []  # second run: nothing pending, failures skipped


def test_scrape_site_stops_when_home_page_is_dead(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_fetch_page(session: object, website: str, path: str, firecrawl: object) -> None:
        calls.append(path)
        return None

    monkeypatch.setattr(sw, "fetch_page", fake_fetch_page)
    assert sw.scrape_site("https://dead.example.in", None) == []
    assert calls == ["/"]


def test_parked_domain_is_not_saved(monkeypatch: pytest.MonkeyPatch) -> None:
    parked = "<html><body><p>Registered at Hostinger. If this is your domain, you can manage it in your " \
             "Hostinger account. Parked Domain name on Hostinger DNS system.</p>" + "<p>filler text</p>" * 40 + \
             "</body></html>"
    monkeypatch.setattr(sw, "fetch_html", lambda s, u: (200, u, parked))
    fc = FakeFirecrawl()
    assert sw.fetch_page(object(), "http://parked.example.in", "/", fc) is None  # type: ignore[arg-type]
    assert fc.calls == []
