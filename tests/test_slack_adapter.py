"""Unit tests for the Slack transport adapter and Block Kit builders."""

from unittest.mock import MagicMock, patch
import pytest

from promise_keeper.adapters.slack import (
    SlackAdapter,
    build_promise_card,
    build_reminder_card,
)
from promise_keeper.models import (
    ActionResult,
    ContextMessage,
    NormalizedEvent,
    PipelineResult,
    PromiseCardData,
    ReminderNotification,
    UserAction,
)


def test_build_promise_card_pending_confirmation() -> None:
    card = PromiseCardData(
        promise_id="p-123",
        owner_id="U12345",
        action="send the designs",
        deadline_text="by noon",
        status="pending_confirmation",
    )
    blocks = build_promise_card(card)

    assert len(blocks) == 2
    assert blocks[0]["type"] == "section"
    assert "<@U12345>" in blocks[0]["text"]["text"]
    assert "send the designs" in blocks[0]["text"]["text"]
    assert "by noon" in blocks[0]["text"]["text"]
    assert "Pending Confirmation" in blocks[0]["text"]["text"]

    actions = blocks[1]["elements"]
    assert len(actions) == 2
    assert actions[0]["action_id"] == "promise_confirm"
    assert actions[0]["value"] == "p-123"
    assert actions[1]["action_id"] == "promise_dismiss"
    assert actions[1]["value"] == "p-123"


def test_build_promise_card_confirmed() -> None:
    card = PromiseCardData(
        promise_id="p-123",
        owner_id="U12345",
        action="build the API",
        status="confirmed",
    )
    blocks = build_promise_card(card)

    assert len(blocks) == 2
    assert "Confirmed" in blocks[0]["text"]["text"]
    actions = blocks[1]["elements"]
    assert len(actions) == 2
    assert actions[0]["action_id"] == "promise_complete"
    assert actions[1]["action_id"] == "promise_snooze"


def test_build_promise_card_completed_has_no_actions() -> None:
    card = PromiseCardData(
        promise_id="p-123",
        owner_id="U12345",
        action="build the API",
        status="completed",
    )
    blocks = build_promise_card(card)

    # Completed cards should only have the info section, no buttons
    assert len(blocks) == 1
    assert "Completed" in blocks[0]["text"]["text"]


def test_build_reminder_card() -> None:
    notification = ReminderNotification(
        promise_id="p-456",
        owner_id="U99999",
        action="review pull request",
        deadline_text="today 5pm",
    )
    blocks = build_reminder_card(notification)

    assert len(blocks) == 2
    assert "review pull request" in blocks[0]["text"]["text"]
    assert "today 5pm" in blocks[0]["text"]["text"]
    assert blocks[1]["elements"][0]["action_id"] == "promise_complete"


@patch("promise_keeper.adapters.slack.SocketModeHandler")
@patch("promise_keeper.adapters.slack.App")
def test_slack_adapter_ignores_bot_messages(mock_app_cls: MagicMock, mock_handler_cls: MagicMock) -> None:
    mock_app = MagicMock()
    mock_app.client.auth_test.return_value = {"user_id": "UBOT123", "team_id": "T123", "team": "Demo"}
    mock_app_cls.return_value = mock_app

    mock_process = MagicMock()
    adapter = SlackAdapter(
        bot_token="xoxb-dummy",
        app_token="xapp-dummy",
        process_event_fn=mock_process,
    )

    # Case 1: bot_id is present
    adapter._handle_inbound_message({"bot_id": "B123", "text": "I am a bot"})
    mock_process.assert_not_called()

    # Case 2: author is the bot itself
    adapter._handle_inbound_message({"user": "UBOT123", "text": "Echo message"})
    mock_process.assert_not_called()

    # Case 3: unwanted subtype
    adapter._handle_inbound_message({"user": "UUSER1", "subtype": "channel_join", "text": "joined"})
    mock_process.assert_not_called()


@patch("promise_keeper.adapters.slack.SocketModeHandler")
@patch("promise_keeper.adapters.slack.App")
def test_slack_adapter_normalizes_and_dispatches_message(mock_app_cls: MagicMock, mock_handler_cls: MagicMock) -> None:
    mock_app = MagicMock()
    mock_app.client.auth_test.return_value = {"user_id": "UBOT123", "team_id": "T123", "team": "Demo"}
    mock_app_cls.return_value = mock_app

    captured_event = None

    def fake_process(event: NormalizedEvent) -> PipelineResult:
        nonlocal captured_event
        captured_event = event
        return PipelineResult(
            processed=True,
            should_respond=True,
            promise_card=PromiseCardData(
                promise_id="p-1",
                owner_id=event.author_id,
                action="deliver slides",
                status="pending_confirmation",
            ),
        )

    adapter = SlackAdapter(
        bot_token="xoxb-dummy",
        app_token="xapp-dummy",
        process_event_fn=fake_process,
    )

    raw_event = {
        "channel": "C12345",
        "user": "UALICE",
        "text": "I will deliver slides by tomorrow",
        "ts": "1710000000.000100",
    }
    adapter._handle_inbound_message(raw_event)

    assert captured_event is not None
    assert captured_event.channel_id == "C12345"
    assert captured_event.author_id == "UALICE"
    assert captured_event.text == "I will deliver slides by tomorrow"
    assert captured_event.event_ts == "1710000000.000100"
    assert captured_event.workspace_id == "T123"

    # Verify message was posted with Block Kit card
    mock_app.client.chat_postMessage.assert_called_once()
    call_kwargs = mock_app.client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "C12345"
    assert call_kwargs["thread_ts"] == "1710000000.000100"
    assert len(call_kwargs["blocks"]) == 2


@patch("promise_keeper.adapters.slack.SocketModeHandler")
@patch("promise_keeper.adapters.slack.App")
def test_slack_adapter_fetches_thread_context(mock_app_cls: MagicMock, mock_handler_cls: MagicMock) -> None:
    mock_app = MagicMock()
    mock_app.client.auth_test.return_value = {"user_id": "UBOT123", "team_id": "T123", "team": "Demo"}
    mock_app.client.conversations_replies.return_value = {
        "messages": [
            {"user": "UBOB", "text": "Can someone do this?", "ts": "1710000000.000010"},
            {"user": "UALICE", "text": "I will take it", "ts": "1710000000.000020"},
        ]
    }
    mock_app_cls.return_value = mock_app

    captured_event = None

    def fake_process(event: NormalizedEvent) -> PipelineResult:
        nonlocal captured_event
        captured_event = event
        return PipelineResult(processed=True, should_respond=False)

    adapter = SlackAdapter(
        bot_token="xoxb-dummy",
        app_token="xapp-dummy",
        process_event_fn=fake_process,
    )

    raw_event = {
        "channel": "C12345",
        "user": "UALICE",
        "text": "I will take it",
        "ts": "1710000000.000020",
        "thread_ts": "1710000000.000010",
    }
    adapter._handle_inbound_message(raw_event)

    assert captured_event is not None
    assert captured_event.thread_ts == "1710000000.000010"
    # Should include Bob's message but exclude Alice's current message
    assert len(captured_event.context_messages) == 1
    assert captured_event.context_messages[0].user_id == "UBOB"
    assert captured_event.context_messages[0].text == "Can someone do this?"


@patch("promise_keeper.adapters.slack.SocketModeHandler")
@patch("promise_keeper.adapters.slack.App")
def test_slack_adapter_handles_interactive_action_authorized(mock_app_cls: MagicMock, mock_handler_cls: MagicMock) -> None:
    mock_app = MagicMock()
    mock_app.client.auth_test.return_value = {"user_id": "UBOT123", "team_id": "T123", "team": "Demo"}
    mock_app_cls.return_value = mock_app

    def fake_action_handler(action: UserAction) -> ActionResult:
        assert action.action_name == "confirm"
        assert action.promise_id == "p-999"
        assert action.actor_id == "UALICE"
        return ActionResult(
            success=True,
            updated_card=PromiseCardData(
                promise_id="p-999",
                owner_id="UALICE",
                action="deliver slides",
                status="confirmed",
            ),
        )

    adapter = SlackAdapter(
        bot_token="xoxb-dummy",
        app_token="xapp-dummy",
        handle_action_fn=fake_action_handler,
    )

    body = {
        "user": {"id": "UALICE"},
        "channel": {"id": "C12345"},
        "message": {"ts": "1710000000.000099"},
        "actions": [
            {
                "action_id": "promise_confirm",
                "value": "p-999",
            }
        ],
    }

    adapter._handle_interactive_action(body)

    mock_app.client.chat_update.assert_called_once()
    call_kwargs = mock_app.client.chat_update.call_args.kwargs
    assert call_kwargs["channel"] == "C12345"
    assert call_kwargs["ts"] == "1710000000.000099"
    assert "Confirmed" in call_kwargs["blocks"][0]["text"]["text"]


@patch("promise_keeper.adapters.slack.SocketModeHandler")
@patch("promise_keeper.adapters.slack.App")
def test_slack_adapter_handles_interactive_action_unauthorized(mock_app_cls: MagicMock, mock_handler_cls: MagicMock) -> None:
    mock_app = MagicMock()
    mock_app.client.auth_test.return_value = {"user_id": "UBOT123", "team_id": "T123", "team": "Demo"}
    mock_app_cls.return_value = mock_app

    def fake_action_handler(action: UserAction) -> ActionResult:
        return ActionResult(
            success=False,
            error_message="Only the promise owner can confirm this promise.",
        )

    adapter = SlackAdapter(
        bot_token="xoxb-dummy",
        app_token="xapp-dummy",
        handle_action_fn=fake_action_handler,
    )

    body = {
        "user": {"id": "UEVE"},
        "channel": {"id": "C12345"},
        "message": {"ts": "1710000000.000099"},
        "actions": [
            {
                "action_id": "promise_confirm",
                "value": "p-999",
            }
        ],
    }

    adapter._handle_interactive_action(body)

    mock_app.client.chat_update.assert_not_called()
    mock_app.client.chat_postEphemeral.assert_called_once()
    ephemeral_kwargs = mock_app.client.chat_postEphemeral.call_args.kwargs
    assert ephemeral_kwargs["channel"] == "C12345"
    assert ephemeral_kwargs["user"] == "UEVE"
    assert "Only the promise owner" in ephemeral_kwargs["text"]


@patch("promise_keeper.adapters.slack.SocketModeHandler")
@patch("promise_keeper.adapters.slack.App")
def test_slack_adapter_send_owner_reminder(mock_app_cls: MagicMock, mock_handler_cls: MagicMock) -> None:
    mock_app = MagicMock()
    mock_app.client.auth_test.return_value = {"user_id": "UBOT123", "team_id": "T123", "team": "Demo"}
    mock_app.client.conversations_open.return_value = {"channel": {"id": "D_ALICE"}}
    mock_app_cls.return_value = mock_app

    adapter = SlackAdapter(
        bot_token="xoxb-dummy",
        app_token="xapp-dummy",
    )

    notification = ReminderNotification(
        promise_id="p-111",
        owner_id="UALICE",
        action="finish slides",
        deadline_text="12:00",
    )

    success = adapter.send_owner_reminder(notification)
    assert success is True
    mock_app.client.conversations_open.assert_called_once_with(users=["UALICE"])
    mock_app.client.chat_postMessage.assert_called_once()
    call_kwargs = mock_app.client.chat_postMessage.call_args.kwargs
    assert call_kwargs["channel"] == "D_ALICE"
    assert len(call_kwargs["blocks"]) == 2
