"""Tests for batched entity classifier JSON parsing and OpenRouter batch calls."""

from unittest.mock import MagicMock, patch

import pytest

from app.entity.classifier_batch import (
    ClassifyBatchError,
    EntityClassifyResult,
    EntityLeadInput,
    _parse_results,
    classify_entities_batch,
)


def test_parse_results_from_json_object():
    data = {
        "results": [
            {
                "id": 12,
                "entity_type": "business",
                "confidence": 0.92,
                "reason": "Plumbing company page",
            },
            {
                "id": 15,
                "entity_type": "person",
                "confidence": 0.81,
                "reason": "Personal profile",
            },
        ]
    }
    parsed = _parse_results(data, {12, 15})
    assert len(parsed) == 2
    by_id = {r.row_index: r for r in parsed}
    assert by_id[12].entity_type == "business"
    assert by_id[12].confidence == 0.92
    assert by_id[15].entity_type == "person"


def test_parse_results_ignores_unknown_ids_and_duplicates():
    data = {
        "results": [
            {"id": 1, "entity_type": "person", "confidence": 0.9},
            {"id": 99, "entity_type": "person", "confidence": 0.9},
            {"id": 1, "entity_type": "business", "confidence": 0.5},
        ]
    }
    parsed = _parse_results(data, {1})
    assert len(parsed) == 1
    assert parsed[0].row_index == 1
    assert parsed[0].entity_type == "person"


def test_parse_results_clamps_confidence():
    data = {"results": [{"id": 3, "entity_type": "business", "confidence": 1.5}]}
    parsed = _parse_results(data, {3})
    assert parsed[0].confidence == 1.0


def test_parse_results_accepts_top_level_array():
    data = [{"id": 7, "entity_type": "person", "confidence": 0.77, "reason": "friends list"}]
    parsed = _parse_results(data, {7})
    assert len(parsed) == 1
    assert parsed[0].entity_type == "person"


def test_confidence_threshold_screen_vs_classify_defaults():
    """Document default thresholds used by screen/classify services."""
    screen_threshold = 0.88
    classify_threshold = 0.75

    high_person = EntityClassifyResult(10, "person", 0.9, "obvious person")
    low_person = EntityClassifyResult(11, "person", 0.7, "maybe person")

    assert high_person.confidence >= screen_threshold
    assert low_person.confidence < screen_threshold
    assert high_person.confidence >= classify_threshold
    assert low_person.confidence < classify_threshold


@patch("app.entity.classifier_batch.httpx.Client")
def test_classify_entities_batch_screen_mode(mock_client_cls):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"results": [{"id": 5, "entity_type": "business", '
                        '"confidence": 0.91, "reason": "Trade business name"}]}'
                    )
                }
            }
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value.__enter__.return_value = mock_client

    leads = [
        EntityLeadInput(5, "Acme Plumbing", "https://facebook.com/acmeplumbing"),
    ]
    results = classify_entities_batch(
        leads,
        mode="screen",
        api_key="test-key",
        model="openai/gpt-4o-mini",
        base_url="https://openrouter.ai/api/v1",
    )

    assert len(results) == 1
    assert results[0].entity_type == "business"
    assert results[0].confidence == 0.91

    call_kwargs = mock_client.post.call_args
    payload = call_kwargs.kwargs["json"]
    assert payload["model"] == "openai/gpt-4o-mini"
    assert "display name and URL" in payload["messages"][0]["content"]


@patch("app.entity.classifier_batch.httpx.Client")
def test_classify_entities_batch_full_mode(mock_client_cls):
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"results": [{"id": 8, "entity_type": "person", '
                        '"confidence": 0.8, "reason": "Friends section visible"}]}'
                    )
                }
            }
        ]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value.__enter__.return_value = mock_client

    leads = [
        EntityLeadInput(
            8,
            "Jane Doe",
            "https://facebook.com/profile.php?id=123",
            scrape_text="Friends · Personal details · Photos",
        ),
    ]
    results = classify_entities_batch(
        leads,
        mode="full",
        api_key="test-key",
        model="openai/gpt-4o-mini",
        base_url="https://openrouter.ai/api/v1",
    )

    assert results[0].entity_type == "person"
    payload = mock_client.post.call_args.kwargs["json"]
    assert "scraped page text" in payload["messages"][0]["content"].lower()


@patch("app.entity.classifier_batch.httpx.Client")
def test_classify_entities_batch_batches_ten_leads(mock_client_cls):
    mock_response = MagicMock()
    results_json = {
        "results": [
            {"id": i, "entity_type": "business", "confidence": 0.85, "reason": "ok"}
            for i in range(1, 11)
        ]
    }
    mock_response.json.return_value = {
        "choices": [{"message": {"content": __import__("json").dumps(results_json)}}]
    }
    mock_response.raise_for_status = MagicMock()

    mock_client = MagicMock()
    mock_client.post.return_value = mock_response
    mock_client_cls.return_value.__enter__.return_value = mock_client

    leads = [
        EntityLeadInput(i, f"Biz {i}", f"https://facebook.com/biz{i}")
        for i in range(1, 11)
    ]
    parsed = classify_entities_batch(
        leads,
        mode="screen",
        api_key="test-key",
        model="openai/gpt-4o-mini",
        base_url="https://openrouter.ai/api/v1",
    )

    assert len(parsed) == 10
    user_content = mock_client.post.call_args.kwargs["json"]["messages"][1]["content"]
    assert "10 total" in user_content


@patch("app.entity.classifier_batch.httpx.Client")
def test_classify_entities_batch_http_error(mock_client_cls):
    import httpx

    mock_client = MagicMock()
    mock_client.post.side_effect = httpx.HTTPError("connection failed")
    mock_client_cls.return_value.__enter__.return_value = mock_client

    with pytest.raises(ClassifyBatchError, match="OpenRouter request failed"):
        classify_entities_batch(
            [EntityLeadInput(1, "Test", "https://facebook.com/test")],
            mode="screen",
            api_key="test-key",
            model="openai/gpt-4o-mini",
            base_url="https://openrouter.ai/api/v1",
        )
