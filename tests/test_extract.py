from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from src import extract as ex
from tests.conftest import SAMPLE_SITE_MD


def test_gstin_checksum() -> None:
    assert ex.gstin_is_valid("27AAACR5055K1Z7")
    assert not ex.gstin_is_valid("27AAACR5055K1Z8")
    assert not ex.gstin_is_valid("not-a-gstin")


def test_rules_extract_sample_site() -> None:
    rec = ex.extract_rules(SAMPLE_SITE_MD, "shreejute.in")
    ex.validate_record(rec)
    assert rec["owner_name"] == "Ramesh Kumar Agarwal"
    assert rec["emails"][:2] == ["info@shreejute.in", "sales@shreejute.in"]
    assert "shreejute@gmail.com" in rec["emails"]
    assert not any(e.endswith(".png") for e in rec["emails"])
    assert rec["whatsapp_number"] == "+919829012345"
    assert rec["gstin"] == "27AAACR5055K1Z7"  # the 08... value on the page fails the checksum
    assert rec["year_established"] == 2009
    assert {"jute bags", "promotional bags", "juco bags", "laminated jute", "jute fabric / rolls"} <= set(
        rec["product_mix"]
    )
    assert rec["mentions_jute_roll_input"] is True
    assert rec["size_signal"] == "mid"  # 60 workers
    assert "likely buyer" in rec["notes_for_sales"]
    assert "+919414011111" in rec["notes_for_sales"]


def test_rules_extract_empty_page_is_schema_valid() -> None:
    rec = ex.extract_rules("Welcome to our website.", "x.in")
    ex.validate_record(rec)
    assert rec["owner_name"] is None and rec["emails"] == [] and rec["size_signal"] == "small"
    assert rec["mentions_jute_roll_input"] is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Founder: Mrs. Sunita Sharma", "Sunita Sharma"),
        ("Mr. Vikram Singh Rathore, Managing Director", "Vikram Singh Rathore"),
        ("CEO Name | Anil Jain", "Anil Jain"),
        ("Contact Our Team Today", None),
        ("Director - Jute Bags Division", None),
    ],
)
def test_owner_patterns(text: str, expected: str | None) -> None:
    assert ex.extract_owner(text) == expected


def test_size_from_indiamart_style_facts() -> None:
    size, evidence = ex.estimate_size("Total Number of Employees 26 to 50 People. Annual Turnover 5 - 25 Cr")
    assert size == "mid"
    assert evidence


def test_strict_schema_shape() -> None:
    schema = ex.EXTRACTION_SCHEMA
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    assert ex.LLM_TOOL["strict"] is True


def test_validate_record_rejects_bad_records() -> None:
    good = ex.extract_rules(SAMPLE_SITE_MD, "shreejute.in")
    with pytest.raises(ValueError):
        ex.validate_record({k: v for k, v in good.items() if k != "gstin"})
    with pytest.raises(ValueError):
        ex.validate_record({**good, "size_signal": "huge"})
    with pytest.raises(ValueError):
        ex.validate_record({**good, "extra": 1})


class FakeMessages:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.kwargs: dict[str, Any] = {}

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.kwargs = kwargs
        block = SimpleNamespace(type="tool_use", name="record_company", input=self.payload)
        return SimpleNamespace(stop_reason="tool_use", content=[block])


def test_llm_extractor_uses_strict_tool_and_validates(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = ex.extract_rules(SAMPLE_SITE_MD, "shreejute.in")
    payload["whatsapp_number"] = "098290 12345"
    payload["gstin"] = "08AAACR5055K1Z3"  # bad checksum -> dropped
    messages = FakeMessages(payload)
    extractor = ex.LLMExtractor.__new__(ex.LLMExtractor)
    extractor.model = ex.LLM_MODEL
    extractor._client = SimpleNamespace(beta=SimpleNamespace(messages=messages))  # type: ignore[attr-defined]
    rec = extractor(SAMPLE_SITE_MD, "shreejute.in")
    assert rec["whatsapp_number"] == "+919829012345"
    assert rec["gstin"] is None
    sent = messages.kwargs
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["tools"][0]["strict"] is True
    assert "tool_choice" not in sent  # forced tool_choice is rejected by this model


def test_run_is_resumable(tmp_path: Path) -> None:
    src_dir, out_dir = tmp_path / "web", tmp_path / "enriched"
    src_dir.mkdir()
    (src_dir / "shreejute.in.md").write_text(SAMPLE_SITE_MD, encoding="utf-8")
    (src_dir / "other.in.md").write_text("website: https://other.in\n\nJute bags.", encoding="utf-8")
    (src_dir / "_failed.json").write_text("{}", encoding="utf-8")
    written = ex.run(in_dir=src_dir, out_dir=out_dir)
    assert sorted(p.name for p in written) == ["other.in.json", "shreejute.in.json"]
    rec = json.loads((out_dir / "shreejute.in.json").read_text(encoding="utf-8"))
    assert rec["domain"] == "shreejute.in" and rec["extraction_method"] == "rules"
    assert ex.run(in_dir=src_dir, out_dir=out_dir) == []
    assert len(ex.run(in_dir=src_dir, out_dir=out_dir, force=True)) == 2
