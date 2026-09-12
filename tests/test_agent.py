import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from promise_keeper.agent import interpret_message
from promise_keeper.models import NormalizedEvent


@pytest.fixture
def event() -> NormalizedEvent:
    source = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    return NormalizedEvent(event_id="agent-event", workspace_id="T1", channel_id="C1", author_id="alice",
                           text="Yes, tomorrow.", event_ts=f"{int(source.timestamp())}.000001",
                           received_at=datetime(2026, 9, 15, 10, tzinfo=timezone.utc))


@pytest.fixture
def client():
    instance = MagicMock()
    instance.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content='{"operation":"ignore"}'))],
    )
    return instance


def test_model_gets_source_time_and_bounded_json_contract(event, client) -> None:
    result = interpret_message(event, [], client, "configured-model", "Europe/Berlin")
    assert result.operation == "ignore"
    arguments = client.chat.completions.create.call_args.kwargs
    assert arguments["model"] == "configured-model"
    assert arguments["response_format"] == {"type": "json_object"}
    assert arguments["max_tokens"] == 1000
    payload = json.loads(arguments["messages"][1]["content"])
    assert payload["occurred_at"].startswith("2026-09-12T12:00:00")
    assert payload["occurred_at"].endswith("+02:00")
    assert payload["author_id"] == "alice"
    assert "received_at" not in payload
    assert "owner_id" not in payload["schema"]["properties"]
    instructions = arguments["messages"][0]["content"]
    assert "untrusted" in instructions
    assert "quotations" in instructions
    assert "hypotheticals" in instructions
    assert "unaccepted requests" in instructions


def test_dependency_cues_require_a_candidate_match(event, client) -> None:
    interpret_message(event, [], client, "configured-model", "UTC")

    instructions = client.chat.completions.create.call_args.kwargs["messages"][0]["content"]

    assert "but first" in instructions
    assert "после" in instructions
    assert "MUST set depends_on_promise_id" in instructions


@pytest.mark.parametrize("content", [None, "not JSON", '{"operation":"create","action":"Send designs"}', '{"operation":"ignore","owner_id":"bob"}'])
def test_invalid_model_responses_are_rejected(event, client, content) -> None:
    client.chat.completions.create.return_value.choices[0].message.content = content
    with pytest.raises((ValidationError, ValueError)):
        interpret_message(event, [], client, "configured-model", "UTC")


def test_truncated_decision_is_not_executed(event, client) -> None:
    client.chat.completions.create.return_value.choices[0].finish_reason = "length"
    with pytest.raises(ValueError, match="Incomplete"):
        interpret_message(event, [], client, "configured-model", "UTC")
