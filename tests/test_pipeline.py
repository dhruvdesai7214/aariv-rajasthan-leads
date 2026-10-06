from __future__ import annotations

import argparse

import pytest

from src import pipeline


def test_parse_cities() -> None:
    assert pipeline.parse_cities("Jaipur, jodhpur,kishangarh") == ["jaipur", "jodhpur", "kishangarh"]
    assert pipeline.parse_cities(None) is None
    with pytest.raises(argparse.ArgumentTypeError):
        pipeline.parse_cities("jaipur,mumbai")


def test_parse_steps_keeps_pipeline_order() -> None:
    assert pipeline.parse_steps("merge,dedupe") == ["dedupe", "merge"]
    with pytest.raises(argparse.ArgumentTypeError):
        pipeline.parse_steps("deploy")


def test_run_calls_steps_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(pipeline.scrape_gmaps, "run", lambda cities: calls.append(f"gmaps:{cities}"))
    monkeypatch.setattr(pipeline.dedupe, "run", lambda cities: calls.append("dedupe"))
    monkeypatch.setattr(pipeline.scrape_websites, "run", lambda **kw: calls.append("websites"))
    monkeypatch.setattr(pipeline.extract, "run", lambda use_llm: calls.append(f"extract:{use_llm}"))
    monkeypatch.setattr(pipeline.merge, "run", lambda: calls.append("merge"))
    pipeline.run(cities=["jaipur"], use_llm=False)
    assert calls == ["gmaps:['jaipur']", "dedupe", "websites", "extract:False", "merge"]
