from __future__ import annotations

import pytest

from src.common import load_config
from src.scrape_gmaps import SearchQuery, build_actor_input, build_queries


def test_build_queries_expands_templates_per_city() -> None:
    config = load_config()
    queries = build_queries(config, ["jaipur", "kishangarh"])
    assert {q.city_key for q in queries} == {"jaipur", "kishangarh"}
    assert all("{city}" not in q.query for q in queries)
    n_templates = len(config["templates"])
    extras = config["city_extra_templates"]["jaipur"]
    assert len(queries) == 2 * n_templates + len(extras)
    assert len({q.slug for q in queries}) == len(queries)


def test_build_queries_rejects_unknown_city() -> None:
    with pytest.raises(ValueError, match="Unknown cities"):
        build_queries(load_config(), ["mumbai"])


def test_actor_input_keeps_paid_addons_off() -> None:
    q = SearchQuery("jaipur", "Jaipur", "Rajasthan", "jute bag manufacturer in Jaipur")
    run_input = build_actor_input(q, {"max_places_per_query": 10})
    assert run_input["searchStringsArray"] == [q.query]
    assert run_input["maxCrawledPlacesPerSearch"] == 10
    assert run_input["scrapeContacts"] is False
    assert run_input["scrapePlaceDetailPage"] is False
    assert run_input["maxReviews"] == 0 and run_input["maxImages"] == 0
    assert run_input["skipClosedPlaces"] is False
