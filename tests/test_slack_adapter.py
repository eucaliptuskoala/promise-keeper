from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

from promise_keeper.adapters.slack import SlackAdapter, build_promise_card
from promise_keeper.models import ActionResult, CardUpdate, PipelineResult, PromiseCardData, ReminderNotification


@pytest.fixture
def adapter():
    with patch("promise_keeper.adapters.slack.App") as app_class, patch("promise_keeper.adapters.slack.SocketModeHandler"):
        app = MagicMock()
        app.client.auth_test.return_value = {"user_id": "bot", "team_id": "T1"}
        app.client.conversations_history.return_value = {"messages": []}
        app.client.chat_postMessage.return_value = {"ts": "1789207201.000001"}
        app.client.chat_update.return_value = {"ts": "1789207201.000001"}
        app_class.return_value = app
        instance = SlackAdapter(
            bot_token="synthetic-bot-token", app_token="synthetic-app-token",
            process_event_fn=MagicMock(return_value=PipelineResult(status="ignored")),
            handle_action_fn=MagicMock(), enabled_channels=("C1",),
            record_delivery_fn=MagicMock(), record_reminder_fn=MagicMock(),
        )
        yield instance


@pytest.fixture
def card() -> PromiseCardData:
    return PromiseCardData(promise_id="p-1", owner_id="alice", action="Send designs", deadline_text="by noon")


@pytest.fixture
def body() -> dict:
    return {"team": {"id": "T1"}, "user": {"id": "alice"}, "channel": {"id": "C1"},
            "message": {"ts": "1789207201.000001"},
            "actions": [{"action_id": "promise_confirm", "value": "p-1", "action_ts": "1789207260.000001"}]}


def test_card_buttons_match_lifecycle(card) -> None:
    pending = build_promise_card(card)
    assert [button["action_id"] for button in pending[1]["elements"]] == ["promise_confirm", "promise_dismiss"]
    confirmed = build_promise_card(card.model_copy(update={"status": "confirmed"}))
    assert [button["action_id"] for button in confirmed[1]["elements"]] == ["promise_complete", "promise_reschedule"]
    assert len(build_promise_card(card.model_copy(update={"status": "completed"}))) == 1
    assert len(build_promise_card(card.model_copy(update={"status": "dismissed"}))) == 1


def test_reminder_card_has_snooze_button() -> None:
    from promise_keeper.adapters.slack import build_reminder_card
    notification = ReminderNotification(
        promise_id="p-1", workspace_id="T1", owner_id="alice", action="Send designs", channel_id="C1",
        thread_ts="1789207200.000001",
    )
    reminder_blocks = build_reminder_card(notification)
    assert [button["action_id"] for button in reminder_blocks[1]["elements"]] == ["promise_complete", "promise_snooze"]


def test_untrusted_card_text_cannot_inject_mentions(card) -> None:
    hostile = card.model_copy(update={"action": "Send <!channel> <@bob> & designs"})
    text = build_promise_card(hostile)[0]["text"]["text"]
    assert "<!channel>" not in text
    assert "<@bob>" not in text
    assert "<@alice>" in text
    assert "&amp;" in text


def test_cards_stay_within_section_text_limit(card) -> None:
    oversized = card.model_copy(update={"action": "&" * 2000, "deadline_text": "&" * 1000})
    assert len(build_promise_card(oversized)[0]["text"]["text"]) <= 3000


@pytest.mark.parametrize("event", [
    {"bot_id": "B1", "user": "alice", "text": "I'll send it", "channel": "C1"},
    {"user": "bot", "text": "I'll send it", "channel": "C1"},
    {"user": "alice", "text": "joined", "subtype": "channel_join", "channel": "C1"},
    {"user": "alice", "text": "I'll send it", "channel": "C2"},
    {"user": "alice", "text": " ", "channel": "C1"},
])
def test_adapter_ignores_unsupported_or_disabled_messages(adapter, event) -> None:
    adapter._handle_inbound_message(event)
    adapter.process_event_fn.assert_not_called()


def test_normalization_and_delivery_are_acknowledged(adapter, card) -> None:
    adapter.process_event_fn.return_value = PipelineResult(promise_card=card)
    adapter._handle_inbound_message({"channel": "C1", "user": "alice", "text": "I'll send designs", "ts": "1789207200.000001"}, "Ev1")
    event = adapter.process_event_fn.call_args.args[0]
    assert event.event_id == "Ev1"
    assert event.workspace_id == "T1"
    assert event.author_id == "alice"
    adapter.app.client.chat_postMessage.assert_called_once()
    assert adapter.app.client.chat_postMessage.call_args.kwargs["thread_ts"] == event.event_ts
    adapter.record_delivery_fn.assert_called_once_with("Ev1", "message", "1789207201.000001")


def test_failed_delivery_is_not_reported_as_sent(adapter, card) -> None:
    adapter.process_event_fn.return_value = PipelineResult(promise_card=card)
    adapter.app.client.chat_postMessage.side_effect = SlackApiError("synthetic", {"error": "ratelimited"})
    adapter._handle_inbound_message({"channel": "C1", "user": "alice", "text": "I'll send designs", "ts": "1789207200.000001"}, "Ev1")
    adapter.record_delivery_fn.assert_called_once_with("Ev1", "message", None)


def test_context_excludes_future_bot_and_current_messages(adapter) -> None:
    adapter.app.client.conversations_replies.return_value = {"messages": [
        {"user": "bob", "text": "Can you send designs?", "ts": "1789207200.000001"},
        {"user": "alice", "text": "Yes", "ts": "1789207202.000001"},
        {"user": "bob", "text": "Future reply", "ts": "1789207203.000001"},
        {"user": "bot", "text": "Card", "ts": "1789207201.000001"},
    ]}
    adapter._handle_inbound_message({"channel": "C1", "user": "alice", "text": "Yes", "ts": "1789207202.000001", "thread_ts": "1789207200.000001"})
    context = adapter.process_event_fn.call_args.args[0].context_messages
    assert len(context) == 1
    assert context[0].user_id == "bob"


def test_bot_thread_permission_failure_fetches_parent(adapter) -> None:
    adapter.app.client.conversations_replies.side_effect = SlackApiError("synthetic", {"error": "missing_scope"})
    adapter.app.client.conversations_history.return_value = {"messages": [
        {"user": "bob", "text": "Send designs?", "ts": "1789207200.000001"},
    ]}
    context = adapter._fetch_thread_context("C1", "1789207200.000001", "1789207202.000001")
    assert context[0].text == "Send designs?"
    assert adapter.app.client.conversations_history.call_args.kwargs["oldest"] == "1789207200.000001"


def test_action_uses_trusted_actor_and_stable_click_id(adapter, body, card) -> None:
    adapter.handle_action_fn.return_value = ActionResult(success=True, updated_card=card.model_copy(update={"status": "confirmed"}))
    adapter._handle_interactive_action(body)
    action = adapter.handle_action_fn.call_args.args[0]
    assert action.actor_id == "alice"
    assert action.workspace_id == "T1"
    assert action.event_id.endswith("promise_confirm:1789207260.000001")
    adapter.app.client.chat_update.assert_called_once()
    adapter.record_delivery_fn.assert_called_once_with(action.event_id, "action", "1789207201.000001")


def test_unauthorized_action_does_not_update_card(adapter, body) -> None:
    adapter.handle_action_fn.return_value = ActionResult(success=False, error_message="Only the promise owner can perform this action.")
    adapter._handle_interactive_action(body)
    adapter.app.client.chat_update.assert_not_called()
    adapter.app.client.chat_postEphemeral.assert_called_once()


def test_completion_updates_unblocked_dependent_cards(adapter, body, card) -> None:
    dependent = card.model_copy(update={"promise_id": "p-2", "owner_id": "bob", "status": "confirmed"})
    adapter.handle_action_fn.return_value = ActionResult(
        success=True,
        updated_card=card.model_copy(update={"status": "completed"}),
        unblocked_card_updates=(CardUpdate(promise_card=dependent, channel_id="C1", message_ts="1789207202.000001"),),
    )
    body["actions"][0]["action_id"] = "promise_complete"

    adapter._handle_interactive_action(body)

    assert adapter.app.client.chat_update.call_count == 2
    assert adapter.app.client.chat_update.call_args_list[1].kwargs["ts"] == "1789207202.000001"


def test_foreign_workspace_action_is_ignored(adapter, body) -> None:
    body["team"]["id"] = "T2"
    adapter._handle_interactive_action(body)
    adapter.handle_action_fn.assert_not_called()


def test_reschedule_opens_modal_without_changing_state(adapter, body) -> None:
    body["trigger_id"] = "synthetic-trigger"
    body["actions"][0]["action_id"] = "promise_reschedule"
    adapter._handle_interactive_action(body)
    adapter.app.client.views_open.assert_called_once()
    adapter.handle_action_fn.assert_not_called()
    assert adapter.app.client.views_open.call_args.kwargs["view"]["blocks"][0]["element"]["type"] == "datetimepicker"


def test_reschedule_submission_passes_typed_deadline(adapter, card) -> None:
    adapter.handle_action_fn.return_value = ActionResult(success=True, updated_card=card.model_copy(update={"status": "confirmed"}))
    body = {"team": {"id": "T1"}, "user": {"id": "alice"}, "view": {
        "id": "view-1", "hash": "hash-1", "private_metadata": '{"promise_id":"p-1","channel_id":"C1","message_ts":"1789207201.000001","occurred_at":"2026-09-12T10:01:00Z"}',
        "state": {"values": {"deadline": {"deadline_at": {"selected_date_time": 1789293600}}}},
    }}
    adapter._handle_reschedule_submission(body)
    action = adapter.handle_action_fn.call_args.args[0]
    assert action.action_name == "reschedule"
    assert action.deadline_at.timestamp() == 1789293600


def test_reschedule_modal_validation_errors(adapter) -> None:
    view_calls = [call for call in adapter.app.view.call_args_list if call.args and call.args[0] == "promise_reschedule_submit"]
    assert view_calls, "promise_reschedule_submit handler not registered"
    decorator = adapter.app.view.return_value
    on_reschedule_fn = decorator.call_args.args[0]

    # 1. Missing date
    mock_ack = MagicMock()
    body_missing = {"view": {"state": {"values": {"deadline": {"deadline_at": {"selected_date_time": None}}}}}}
    on_reschedule_fn(mock_ack, body_missing)
    mock_ack.assert_called_once_with(response_action="errors", errors={"deadline": "Please select a date and time."})

    # 2. Date in the past
    mock_ack.reset_mock()
    body_past = {"view": {"state": {"values": {"deadline": {"deadline_at": {"selected_date_time": 1000000000}}}}}}
    on_reschedule_fn(mock_ack, body_past)
    mock_ack.assert_called_once_with(response_action="errors", errors={"deadline": "Deadline must be in the future."})


def test_private_delivery_records_card_and_never_falls_back_publicly(adapter) -> None:
    notification = ReminderNotification(promise_id="p-1", workspace_id="T1", owner_id="alice", action="Send designs", channel_id="C1", thread_ts="1789207200.000001")
    adapter.app.client.conversations_open.return_value = {"channel": {"id": "D1"}}
    assert adapter.send_owner_reminder(notification)
    assert adapter.app.client.chat_postMessage.call_args.kwargs["channel"] == "D1"
    adapter.record_reminder_fn.assert_called_once_with("p-1", "D1", "1789207201.000001")
    adapter.app.client.chat_postMessage.reset_mock()
    adapter.app.client.conversations_open.side_effect = SlackApiError("synthetic", {"error": "missing_scope"})
    assert not adapter.send_owner_reminder(notification)
    adapter.app.client.chat_postMessage.assert_not_called()


def test_stop_is_idempotent(adapter) -> None:
    adapter.stop()
    adapter.stop()
    adapter.handler.close.assert_called_once()


def test_start_uses_connect_and_can_stop_on_windows(adapter) -> None:
    from threading import Thread

    running = Thread(target=adapter.start, daemon=True)
    running.start()
    adapter.stop()
    running.join(timeout=2)
    assert not running.is_alive()
    adapter.handler.connect.assert_called_once()
    adapter.handler.start.assert_not_called()
