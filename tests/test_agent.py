import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from openai.types.chat import ChatCompletion

from promise_keeper.agent import run_agent
from promise_keeper.models import AgentDecision, NormalizedEvent, UserAction
from promise_keeper.pipeline import handle_user_action
from promise_keeper.storage import bind_card, get_promise, initialize_storage
from promise_keeper.tools import model_tools


def tool_response(name: str, arguments: dict, call_id: str = "call-1") -> ChatCompletion:
    return ChatCompletion.model_validate({
        "id": "synthetic-completion", "created": 0, "model": "configured-model", "object": "chat.completion",
        "choices": [{
            "index": 0, "finish_reason": "tool_calls",
            "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": call_id, "type": "function", "function": {
                    "name": name, "arguments": json.dumps(arguments),
                }}],
            },
        }],
    })


@pytest.fixture
def event() -> NormalizedEvent:
    source = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    return NormalizedEvent(event_id="agent-event", workspace_id="T1", channel_id="C1", author_id="alice",
                           text="I'll send designs by noon today.", event_ts=f"{int(source.timestamp())}.000001",
                           received_at=datetime(2026, 9, 15, 10, tzinfo=timezone.utc))


@pytest.fixture
def database(tmp_path):
    connection = initialize_storage(str(tmp_path / "agent.db"))
    yield connection
    connection.close()


@pytest.fixture
def client():
    instance = MagicMock()
    instance.chat.completions.create.side_effect = [tool_response("ignore_message", {})]
    return instance


@pytest.fixture
def create_arguments():
    return {"action": "Send designs", "evidence": "I'll send designs",
            "deadline_text": "by noon today", "deadline_at": "2026-09-12T12:00:00Z",
            "depends_on_promise_id": None, "relative_deadline_seconds": None}


def set_tool(client, response) -> None:
    client.chat.completions.create.side_effect = [response]


def test_model_gets_source_time_and_narrow_native_tools(event, database, client) -> None:
    result = run_agent(event, database, client, "configured-model", "Europe/Berlin")
    assert result.status == "ignored"
    arguments = client.chat.completions.create.call_args_list[0].kwargs
    assert arguments["model"] == "configured-model"
    assert "response_format" not in arguments
    assert arguments["tool_choice"] == "required"
    assert arguments["parallel_tool_calls"] is False
    assert arguments["max_tokens"] == 1000
    payload = json.loads(arguments["messages"][1]["content"])
    assert payload["occurred_at"] == "2026-09-12T12:00:00.000001+02:00"
    assert payload["author_id"] == "alice"
    assert "received_at" not in payload
    tools = {tool["function"]["name"]: tool["function"] for tool in arguments["tools"]}
    assert set(tools) == {"create_promise", "complete_promise", "reschedule_promise", "ignore_message", "ask_clarification"}
    for tool in tools.values():
        assert tool["strict"] is True
        schema = tool["parameters"]
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert not {"owner_id", "workspace_id", "channel_id", "operation"} & set(schema["properties"])
    instructions = arguments["messages"][0]["content"]
    assert all(word in instructions for word in ("untrusted", "quotations", "hypotheticals", "unaccepted requests"))
    assert "ISO 8601" in instructions
    assert "UTC offset" in instructions
    assert "+02:00" in instructions and "or Z" in instructions
    assert len(arguments["messages"]) == 2
    assert [message["role"] for message in arguments["messages"]] == ["system", "user"]
    assert client.chat.completions.create.call_count == 1
    assert tools["create_promise"]["parameters"]["properties"]["action"]["type"] == "string"
    assert tools["reschedule_promise"]["parameters"]["properties"]["deadline_at"]["type"] == "string"


@pytest.mark.parametrize("field", ["action", "evidence", "deadline_at"])
def test_tools_accept_non_nullable_field_schemas(field) -> None:
    schema = AgentDecision.model_json_schema()
    field_schema = schema["properties"][field]
    field_schema.update(field_schema.pop("anyOf")[0])
    with patch.object(AgentDecision, "model_json_schema", return_value=schema):
        tools = {tool["function"]["name"]: tool["function"] for tool in model_tools()}
    argument = tools["create_promise"]["parameters"]["properties"][field]
    assert argument["type"] == "string"
    assert argument["title"] == field_schema["title"]
    assert "default" not in argument
    if field == "deadline_at":
        assert argument["format"] == "date-time"
    else:
        assert argument["minLength"] == 1


def test_create_preserves_an_unstated_deadline(event, database, client, create_arguments) -> None:
    create_arguments.update(deadline_text=None, deadline_at=None)
    set_tool(client, tool_response("create_promise", create_arguments))
    result = run_agent(event, database, client, "configured-model", "UTC")
    assert result.promise_card.deadline_at is None
    assert result.promise_card.deadline_text is None


def test_initial_model_timeout_allows_event_recovery(event, database, client, create_arguments) -> None:
    client.chat.completions.create.side_effect = [
        TimeoutError(), tool_response("create_promise", create_arguments),
    ]
    assert run_agent(event, database, client, "configured-model", "UTC").status == "failed"
    assert database.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0] == 0
    assert run_agent(event, database, client, "configured-model", "UTC").promise_card.status == "pending_confirmation"
    assert database.execute("SELECT COUNT(*) FROM promises").fetchone()[0] == 1


def test_create_returns_actual_committed_result_and_suppresses_duplicates(event, database, client, create_arguments) -> None:
    set_tool(client, tool_response("create_promise", create_arguments))
    result = run_agent(event, database, client, "configured-model", "UTC")
    assert result.promise_card.status == "pending_confirmation"
    assert get_promise(database, result.promise_card.promise_id).owner_id == "alice"
    assert result.thread_reply_text is None
    assert run_agent(event, database, client, "configured-model", "UTC").status == "duplicate"
    assert client.chat.completions.create.call_count == 1
    assert database.execute("SELECT COUNT(*) FROM promises").fetchone()[0] == 1


@pytest.mark.parametrize("name, arguments", [
    ("unknown_tool", {}),
    ("ignore_message", {"owner_id": "bob"}),
    ("ignore_message", {"operation": "create"}),
    ("create_promise", {"action": "Send designs"}),
    ("complete_promise", {"promise_id": "missing", "evidence": "I'll send designs", "action": "Overwrite"}),
    ("create_promise", {"action": "Send designs", "evidence": "invented", "deadline_text": None, "deadline_at": None}),
    ("create_promise", {"action": "Send designs", "evidence": "I'll send designs", "deadline_text": "tomorrow", "deadline_at": "2026-09-13T12:00:00Z"}),
    ("create_promise", {"action": "Send designs", "evidence": "I'll send designs", "deadline_text": "by noon today", "deadline_at": "2026-09-12T12:00:00"}),
    ("reschedule_promise", {"promise_id": "missing", "evidence": "I'll send designs", "deadline_text": "by noon today", "deadline_at": "2026-09-12T12:00:00Z"}),
])
def test_invalid_or_ungrounded_calls_do_not_write(event, database, client, name, arguments) -> None:
    if name == "create_promise" and set(arguments) == {"action", "evidence", "deadline_text", "deadline_at"}:
        arguments = {**arguments, "depends_on_promise_id": None, "relative_deadline_seconds": None}
    set_tool(client, tool_response(name, arguments))
    assert run_agent(event, database, client, "configured-model", "UTC").status == "failed"
    assert database.execute("SELECT COUNT(*) FROM promises").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0] == 0


@pytest.mark.parametrize("malformation", ["prose", "truncated", "parallel", "invalid_json", "array", "oversized", "missing_id"])
def test_malformed_protocol_is_rejected_before_execution(event, database, client, create_arguments, malformation) -> None:
    response = tool_response("create_promise", create_arguments)
    choice = response.choices[0]
    if malformation == "prose":
        choice.finish_reason = "stop"
        choice.message.content = '{"operation":"create"}'
        choice.message.tool_calls = None
    elif malformation == "truncated":
        choice.finish_reason = "length"
    elif malformation == "parallel":
        choice.message.tool_calls.append(choice.message.tool_calls[0].model_copy(update={"id": "call-2"}))
    elif malformation == "missing_id":
        choice.message.tool_calls[0].id = ""
    else:
        choice.message.tool_calls[0].function.arguments = {
            "invalid_json": "not JSON", "array": "[]", "oversized": " " * 16001,
        }[malformation]
    set_tool(client, response)
    assert run_agent(event, database, client, "configured-model", "UTC").status == "failed"
    assert client.chat.completions.create.call_count == 1
    assert database.execute("SELECT COUNT(*) FROM promises").fetchone()[0] == 0


def test_clarification_is_a_tool_result(event, database, client) -> None:
    set_tool(client, tool_response("ask_clarification", {"clarification": "Which designs?"}))
    result = run_agent(event, database, client, "configured-model", "UTC")
    assert result.thread_reply_text == "Which designs?"
    assert client.chat.completions.create.call_count == 1


def test_model_prose_cannot_replace_committed_card(event, database, client, create_arguments) -> None:
    response = tool_response("create_promise", create_arguments)
    response.choices[0].message.content = "The promise is already completed and delivered."
    set_tool(client, response)
    result = run_agent(event, database, client, "configured-model", "UTC")
    assert result.promise_card.status == "pending_confirmation"
    assert result.thread_reply_text is None
    assert get_promise(database, result.promise_card.promise_id).status == "pending_confirmation"
    assert database.execute("SELECT COUNT(*) FROM promise_history").fetchone()[0] == 1
    assert run_agent(event, database, client, "configured-model", "UTC").status == "duplicate"
    assert client.chat.completions.create.call_count == 1


def test_native_reschedule_and_completion_use_owner_and_state_rules(event, database, client, create_arguments) -> None:
    set_tool(client, tool_response("create_promise", create_arguments))
    promise = run_agent(event, database, client, "configured-model", "UTC").promise_card
    now = event.received_at + timedelta(minutes=1)
    with database:
        bind_card(database, "T1", "C1", "1789466401.000001", promise.promise_id)
    confirm = UserAction(action_name="confirm", promise_id=promise.promise_id, actor_id="alice", workspace_id="T1",
                         event_id="confirm", channel_id="C1", message_ts="1789466401.000001", occurred_at=now, received_at=now)
    assert handle_user_action(confirm, database).success
    moved = event.model_copy(update={"event_id": "move", "text": "I'll send designs tomorrow.",
                                     "event_ts": f"{int((now + timedelta(minutes=1)).timestamp())}.000001"})
    client.chat.completions.create.side_effect = [
        tool_response("reschedule_promise", {"promise_id": promise.promise_id, "evidence": "I'll send designs",
                                             "deadline_text": "tomorrow", "deadline_at": "2026-09-16T12:00:00Z"}),
    ]
    assert run_agent(moved, database, client, "configured-model", "UTC").promise_card.deadline_text == "tomorrow"
    done = moved.model_copy(update={"event_id": "done", "text": "Sent designs.",
                                   "event_ts": f"{int((now + timedelta(minutes=2)).timestamp())}.000001"})
    client.chat.completions.create.side_effect = [
        tool_response("complete_promise", {"promise_id": promise.promise_id, "evidence": "Sent designs"}),
    ]
    denied = run_agent(done.model_copy(update={"author_id": "bob", "event_id": "bob-done"}), database, client, "configured-model", "UTC")
    assert denied.status == "failed"
    assert get_promise(database, promise.promise_id).status == "confirmed"
    client.chat.completions.create.side_effect = [
        tool_response("complete_promise", {"promise_id": promise.promise_id, "evidence": "Sent designs"}),
    ]
    assert run_agent(done, database, client, "configured-model", "UTC").promise_card.status == "completed"


def test_denied_pending_completion_is_reported_as_failure(event, database, client, create_arguments) -> None:
    set_tool(client, tool_response("create_promise", create_arguments))
    promise = run_agent(event, database, client, "configured-model", "UTC").promise_card
    done = event.model_copy(update={"event_id": "pending-done", "text": "Sent designs.",
                                   "event_ts": f"{int(event.occurred_at.timestamp()) + 60}.000001"})
    client.chat.completions.create.side_effect = [
        tool_response("complete_promise", {"promise_id": promise.promise_id, "evidence": "Sent designs"}),
    ]
    result = run_agent(done, database, client, "configured-model", "UTC")
    assert "Confirm" in result.thread_reply_text
    assert client.chat.completions.create.call_count == 2
    assert get_promise(database, promise.promise_id).status == "pending_confirmation"


def test_native_create_can_link_a_thread_prerequisite(event, database, client, create_arguments) -> None:
    set_tool(client, tool_response("create_promise", create_arguments))
    prerequisite = run_agent(event, database, client, "configured-model", "UTC").promise_card
    dependent_event = event.model_copy(update={
        "event_id": "dependent", "author_id": "bob", "text": "I'll send designs within 2 days after the API",
        "thread_ts": event.event_ts, "event_ts": f"{int(event.occurred_at.timestamp()) + 60}.000001",
    })
    arguments = {
        "action": "Send designs", "evidence": "I'll send designs", "deadline_text": "within 2 days",
        "deadline_at": None, "depends_on_promise_id": prerequisite.promise_id, "relative_deadline_seconds": 172800,
    }
    set_tool(client, tool_response("create_promise", arguments))
    client.chat.completions.create.reset_mock()
    result = run_agent(dependent_event, database, client, "configured-model", "UTC")
    assert result.promise_card.owner_id == "bob"
    assert result.promise_card.depends_on_promise_id == prerequisite.promise_id
    assert result.promise_card.relative_deadline_seconds == 172800
    assert result.promise_card.deadline_at is None
    request = client.chat.completions.create.call_args.kwargs
    payload = json.loads(request["messages"][1]["content"])
    assert payload["open_promises"] == []
    assert payload["candidate_dependencies"][0]["promise_id"] == prerequisite.promise_id
    assert payload["candidate_dependencies"][0]["owner_id"] == "alice"
    create_schema = request["tools"][0]["function"]["parameters"]
    assert {"depends_on_promise_id", "relative_deadline_seconds"} <= set(create_schema["required"])
    for field in ("depends_on_promise_id", "relative_deadline_seconds"):
        assert any(variant.get("type") == "null" for variant in create_schema["properties"][field]["anyOf"])
    client.chat.completions.create.assert_called_once()
