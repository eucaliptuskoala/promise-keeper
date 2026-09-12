from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from threading import Event
from unittest.mock import MagicMock, patch

import pytest
from slack_sdk.errors import SlackApiError

from promise_keeper.adapters.slack import SlackAdapter, build_promise_card, build_reminder_card
from promise_keeper.models import ActionResult, AgentDecision, NormalizedEvent, PipelineResult, PromiseCardData, ReminderNotification
from promise_keeper.pipeline import handle_user_action, process_event
from promise_keeper.reminders import check_reminders
from promise_keeper.storage import bind_card, get_promise, initialize_storage, pending_responses, record_delivery


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


def test_all_public_channels_use_pagination_and_exclude_other_conversations() -> None:
    with patch("promise_keeper.adapters.slack.App") as app_class, patch("promise_keeper.adapters.slack.SocketModeHandler"):
        app = app_class.return_value
        app.client.auth_test.return_value = {"user_id": "bot", "team_id": "T1"}
        app.client.conversations_list.side_effect = [
            {"channels": [{"id": "C1", "is_channel": True, "is_private": False}],
             "response_metadata": {"next_cursor": "page-2"}},
            {"channels": [
                {"id": "C2", "is_channel": True, "is_private": False},
                {"id": "private", "is_channel": True, "is_private": True},
                {"id": "archived", "is_channel": True, "is_archived": True},
                {"id": "D1", "is_channel": False},
            ], "response_metadata": {"next_cursor": ""}},
        ]
        instance = SlackAdapter(
            bot_token="synthetic-bot", app_token="synthetic-app", enabled_channels=("*",),
            process_event_fn=MagicMock(return_value=PipelineResult(status="ignored")), handle_action_fn=MagicMock(),
            record_delivery_fn=MagicMock(), record_reminder_fn=MagicMock(),
        )
        assert instance.enabled_channels == ("C1", "C2")
        assert app.client.conversations_list.call_args.kwargs["cursor"] == "page-2"
        assert app.client.conversations_list.call_args.kwargs["types"] == "public_channel"
        for channel_id in ("private", "archived", "D1"):
            instance._handle_inbound_message({"channel": channel_id, "user": "alice", "text": "I'll send it", "ts": "1789207200.000001"})
        instance.process_event_fn.assert_not_called()
        instance._handle_inbound_message({"channel": "C2", "user": "alice", "text": "I'll send it", "ts": "1789207200.000001"})
        instance.process_event_fn.assert_called_once()


@pytest.mark.parametrize("failure", ["missing_scope", "empty", "pagination_limit"])
def test_all_public_channel_lookup_fails_without_broadening_access(failure) -> None:
    with patch("promise_keeper.adapters.slack.App") as app_class, patch("promise_keeper.adapters.slack.SocketModeHandler"):
        app = app_class.return_value
        app.client.auth_test.return_value = {"user_id": "bot", "team_id": "T1"}
        if failure == "missing_scope":
            app.client.conversations_list.side_effect = SlackApiError("synthetic", {"error": "missing_scope"})
        else:
            app.client.conversations_list.return_value = {
                "channels": [], "response_metadata": {"next_cursor": "more" if failure == "pagination_limit" else ""},
            }
        with pytest.raises(SlackApiError if failure == "missing_scope" else ValueError):
            SlackAdapter(
                bot_token="synthetic-bot", app_token="synthetic-app", enabled_channels=("*",),
                process_event_fn=MagicMock(), handle_action_fn=MagicMock(),
                record_delivery_fn=MagicMock(), record_reminder_fn=MagicMock(),
            )
        assert app.client.conversations_list.call_count == (20 if failure == "pagination_limit" else 1)


def test_card_buttons_match_lifecycle(card) -> None:
    assert len(build_promise_card(card)) == 1
    pending = build_promise_card(card, show_controls=True)
    assert [button["action_id"] for button in pending[1]["elements"]] == ["promise_confirm", "promise_dismiss"]
    confirmed = build_promise_card(card.model_copy(update={"status": "confirmed"}), show_controls=True)
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


def test_cards_show_original_deadline_with_exact_localized_time(card) -> None:
    deadline = datetime(2026, 9, 13, 23, 59, tzinfo=timezone(timedelta(hours=2)))
    card = card.model_copy(update={"deadline_text": "tomorrow", "deadline_at": deadline})
    notification = ReminderNotification(
        promise_id=card.promise_id, workspace_id="T1", owner_id=card.owner_id, action=card.action,
        deadline_text=card.deadline_text, deadline_at=deadline, channel_id="C1", thread_ts="1789207200.000001",
    )
    expected = f"tomorrow (<!date^{int(deadline.timestamp())}^{{date_num}} {{time}}|2026-09-13 21:59 UTC>)"
    assert expected in build_promise_card(card)[0]["text"]["text"]
    assert expected in build_reminder_card(notification)[0]["text"]["text"]
    assert "needs clarification" in build_promise_card(card.model_copy(update={"deadline_at": None}))[0]["text"]["text"]


def test_reminder_links_to_channel_and_original_message() -> None:
    notification = ReminderNotification(
        promise_id="p-1", workspace_id="T1", owner_id="alice", action="Send designs",
        channel_id="C1", thread_ts="1789207200.000001", source_message_id="1789207260.000001",
    )
    url = "https://example.slack.com/archives/C1/p1789207260000001?thread_ts=1789207200.000001&cid=C1"
    text = build_reminder_card(notification, url)[0]["text"]["text"]
    assert "*Promised in:* <#C1>" in text
    assert f"<{url.replace('&', '&amp;')}|View original message>" in text


@pytest.mark.parametrize("link_error", [None, "message_not_found", "timeout"])
def test_reminder_delivery_uses_source_message_link_without_blocking_on_lookup_failure(adapter, link_error) -> None:
    notification = ReminderNotification(
        promise_id="p-1", workspace_id="T1", owner_id="alice", action="Send designs",
        deadline_text="tomorrow", deadline_at=datetime(2026, 9, 13, 12, tzinfo=timezone.utc),
        channel_id="C1", thread_ts="1789207200.000001", source_message_id="1789207260.000001",
    )
    url = "https://example.slack.com/archives/C1/p1789207260000001"
    adapter.app.client.conversations_open.return_value = {"channel": {"id": "D1"}}
    adapter.app.client.chat_getPermalink.return_value = {"permalink": url}
    if link_error == "timeout":
        adapter.app.client.chat_getPermalink.side_effect = TimeoutError()
    elif link_error:
        adapter.app.client.chat_getPermalink.side_effect = SlackApiError("synthetic", {"error": link_error})
    assert adapter.send_owner_reminder(notification)
    adapter.app.client.chat_getPermalink.assert_called_once_with(channel="C1", message_ts="1789207260.000001")
    arguments = adapter.app.client.chat_postMessage.call_args.kwargs
    assert arguments["channel"] == "D1"
    assert arguments["unfurl_links"] is False
    assert arguments["unfurl_media"] is False
    text = arguments["blocks"][0]["text"]["text"]
    assert "<#C1>" in text
    assert (url in text) == (link_error is None)
    assert "tomorrow (<!date^" in text
    adapter.record_reminder_fn.assert_called_once()


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


@pytest.mark.parametrize("kind", ["message", "action"])
def test_periodic_retry_does_not_duplicate_inflight_delivery(adapter, body, tmp_path, kind) -> None:
    path = str(tmp_path / "delivery.db")
    now = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    event = NormalizedEvent(event_id="Ev1", workspace_id="T1", channel_id="C1", author_id="alice",
                            text="I'll send designs", event_ts="1789207200.000001", received_at=now)
    decision = AgentDecision(operation="create", action="Send designs", evidence="I'll send designs")

    def process(normalized):
        with closing(initialize_storage(path)) as database:
            return process_event(normalized.model_copy(update={"received_at": now}), database, lambda *_: decision)

    def handle(action):
        with closing(initialize_storage(path)) as database:
            return handle_user_action(action, database)

    def acknowledge(event_id, delivery_kind, message_ts):
        with closing(initialize_storage(path)) as database:
            record_delivery(database, "T1", event_id, delivery_kind, message_ts, now)

    retry_entered = Event()
    ticker_started = Event()
    delivery_started = Event()
    finish_delivery = Event()

    def tick():
        retry_entered.set()
        with closing(initialize_storage(path)) as database:
            for row in pending_responses(database, "T1", now + timedelta(minutes=2)):
                result_type = PipelineResult if row["kind"] in ("message", "owner_card") else ActionResult
                result = result_type.model_validate_json(row["result_json"])
                message_ts = adapter.deliver_result(row["channel_id"], row["target_ts"], row["kind"], result)
                record_delivery(database, "T1", row["event_id"], row["kind"], message_ts, now)

    def send(**kwargs):
        delivery_started.set()
        assert finish_delivery.wait(5)
        return {"ts": "1789207201.000001"}

    def wait(interval):
        ticker_started.set()
        return retry_entered.is_set()

    adapter.process_event_fn = process
    adapter.handle_action_fn = handle
    adapter.record_delivery_fn = acknowledge
    adapter.tick_fn = tick
    adapter._stopped.wait = MagicMock(side_effect=wait)
    adapter.app.client.chat_postMessage.side_effect = send
    adapter.app.client.chat_update.side_effect = send
    adapter.app.client.conversations_open.return_value = {"channel": {"id": "D1"}}
    if kind == "action":
        result = process(event)
        acknowledge(event.event_id, "message", "1789207201.000001")
        acknowledge(event.event_id, "owner_card", "1789207202.000001")
        body["actions"][0]["value"] = result.promise_card.promise_id
        handler = adapter.app.action.return_value.call_args.args[0]
        arguments = (MagicMock(), body)
    else:
        handler = adapter.app.event.return_value.call_args.args[0]
        arguments = ({"channel": "C1", "user": "alice", "text": event.text, "ts": event.event_ts}, {"event_id": event.event_id})
    with ThreadPoolExecutor(max_workers=2) as workers:
        delivery = workers.submit(handler, *arguments)
        try:
            assert delivery_started.wait(5)
            ticker = workers.submit(adapter._run_ticks)
            assert ticker_started.wait(5)
            assert not retry_entered.wait(0.1)
        finally:
            finish_delivery.set()
        delivery.result(timeout=5)
        ticker.result(timeout=5)
    assert adapter.app.client.chat_postMessage.call_count + adapter.app.client.chat_update.call_count == (2 if kind == "message" else 1)
    with closing(initialize_storage(path)) as database:
        assert not pending_responses(database, "T1", now + timedelta(minutes=2))


def test_completion_is_preserved_when_reminder_tick_reads_old_state(adapter, body, tmp_path) -> None:
    path = str(tmp_path / "reminder.db")
    now = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    event = NormalizedEvent(event_id="Ev1", workspace_id="T1", channel_id="C1", author_id="alice",
                            text="I'll send designs by noon", event_ts="1789207200.000001", received_at=now)
    decision = AgentDecision(operation="create", action="Send designs", evidence="I'll send designs",
                             deadline_text="by noon", deadline_at=now + timedelta(hours=2))
    with closing(initialize_storage(path)) as database:
        result = process_event(event, database, lambda *_: decision)
        promise_id = result.promise_card.promise_id
        with database:
            bind_card(database, "T1", "C1", body["message"]["ts"], promise_id)

    def handle(action):
        with closing(initialize_storage(path)) as database:
            return handle_user_action(action, database)

    adapter.handle_action_fn = handle
    body["actions"][0]["value"] = promise_id
    adapter._handle_interactive_action(body)
    body["actions"][0].update(action_id="promise_complete", action_ts="1789207320.000001")
    snapshot_read = Event()
    finish_tick = Event()
    completion_started = Event()
    completion_finished = Event()

    def read(database, selected_id):
        promise = get_promise(database, selected_id)
        if not snapshot_read.is_set():
            snapshot_read.set()
            assert finish_tick.wait(5)
        return promise

    def tick():
        with closing(initialize_storage(path)) as database:
            check_reminders(database, lambda *_: True, now + timedelta(hours=3), "T1", ("C1",))

    def complete():
        completion_started.set()
        adapter._handle_interactive_action(body)
        completion_finished.set()

    adapter.tick_fn = tick
    adapter._stopped.wait = MagicMock(side_effect=[False, True])
    with patch("promise_keeper.reminders.get_promise", side_effect=read), ThreadPoolExecutor(max_workers=2) as workers:
        ticker = workers.submit(adapter._run_ticks)
        try:
            assert snapshot_read.wait(5)
            completion = workers.submit(complete)
            assert completion_started.wait(5)
            assert not completion_finished.wait(0.1)
        finally:
            finish_tick.set()
        ticker.result(timeout=5)
        completion.result(timeout=5)
    with closing(initialize_storage(path)) as database:
        assert get_promise(database, promise_id).status == "completed"


def test_reschedule_modal_opens_while_processing_is_busy(adapter, body) -> None:
    body["trigger_id"] = "synthetic-trigger"
    body["actions"][0]["action_id"] = "promise_reschedule"
    handler = adapter.app.action.return_value.call_args.args[0]
    ack = MagicMock()
    with ThreadPoolExecutor(max_workers=1) as workers:
        with adapter._processing_lock:
            opened = workers.submit(handler, ack, body)
            try:
                opened.result(timeout=2)
            finally:
                ack.assert_called_once()
    adapter.app.client.views_open.assert_called_once()
    adapter.handle_action_fn.assert_not_called()


@pytest.mark.parametrize("failed_channel", [None, "C1", "D1"])
def test_private_controls_and_public_status_share_persisted_lifecycle(adapter, tmp_path, failed_channel) -> None:
    from promise_keeper.__main__ import main
    from promise_keeper.config import Settings
    from promise_keeper.models import UserAction

    now = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    settings = Settings(
        openai_api_key="synthetic-key", slack_bot_token="synthetic-bot", slack_app_token="synthetic-app",
        enabled_channels=("C1",), database_path=str(tmp_path / "private-controls.db"),
    )
    decision = AgentDecision(operation="create", action="Send designs", evidence="I'll send designs")
    delivered = []
    failed = False

    def send(**arguments):
        nonlocal failed
        if arguments["channel"] == failed_channel and not failed:
            failed = True
            raise SlackApiError("synthetic", {"error": "ratelimited"})
        delivered.append(arguments)
        return {"ts": "1789207201.000001" if arguments["channel"] == "C1" else "1789207202.000001"}

    def wire(**callbacks):
        for name in ("process_event_fn", "handle_action_fn", "record_delivery_fn", "record_reminder_fn", "tick_fn"):
            setattr(adapter, name, callbacks[name])
        return adapter

    adapter.app.client.conversations_open.return_value = {"channel": {"id": "D1"}}
    adapter.app.client.chat_getPermalink.return_value = {"permalink": "https://example.slack.com/archives/C1/p1789207200000001"}
    adapter.app.client.chat_postMessage.side_effect = send

    def run():
        adapter.app.client.chat_update.side_effect = lambda **arguments: {"ts": arguments["ts"]}
        adapter._handle_inbound_message(
            {"channel": "C1", "user": "alice", "text": "I'll send designs", "ts": "1789207200.000001"}, "Ev1",
        )
        clock.now.return_value = now + timedelta(seconds=31)
        adapter.tick_fn()
        assert [arguments["channel"] for arguments in delivered].count("C1") == 1
        assert [arguments["channel"] for arguments in delivered].count("D1") == 1
        public = next(arguments for arguments in delivered if arguments["channel"] == "C1")
        private = next(arguments for arguments in delivered if arguments["channel"] == "D1")
        assert "thread_ts" in public and "thread_ts" not in private
        assert len(public["blocks"]) == 1
        assert [button["action_id"] for button in private["blocks"][1]["elements"]] == ["promise_confirm", "promise_dismiss"]
        assert "<#C1>" in private["blocks"][0]["text"]["text"]
        assert "View original message" in private["blocks"][0]["text"]["text"]
        promise_id = private["blocks"][1]["elements"][0]["value"]
        click = {"team": {"id": "T1"}, "user": {"id": "bob"}, "channel": {"id": "D1"},
                 "message": {"ts": "1789207202.000001"},
                 "actions": [{"action_id": "promise_confirm", "value": promise_id, "action_ts": "1789207260.000001"}]}
        adapter._handle_interactive_action(click)
        adapter.app.client.chat_update.assert_not_called()
        click["user"]["id"] = "alice"
        click["actions"][0]["action_ts"] = "1789207261.000001"
        adapter._handle_interactive_action(click)
        updates = {call.kwargs["channel"]: call.kwargs for call in adapter.app.client.chat_update.call_args_list}
        assert set(updates) == {"C1", "D1"}
        assert len(updates["C1"]["blocks"]) == 1
        assert "Confirmed" in updates["C1"]["blocks"][0]["text"]["text"]
        assert [button["action_id"] for button in updates["D1"]["blocks"][1]["elements"]] == ["promise_complete", "promise_reschedule"]
        adapter.app.client.chat_update.reset_mock()
        click["actions"][0].update(action_id="promise_complete", action_ts="1789207320.000001")
        adapter._handle_interactive_action(click)
        updates = {call.kwargs["channel"]: call.kwargs for call in adapter.app.client.chat_update.call_args_list}
        assert set(updates) == {"C1", "D1"}
        assert all(len(update["blocks"]) == 1 and "Completed" in update["blocks"][0]["text"]["text"] for update in updates.values())
        with closing(initialize_storage(settings.database_path)) as database:
            assert get_promise(database, promise_id).status == "completed"
            assert not pending_responses(database, "T1", now + timedelta(days=1))
            bindings = database.execute("SELECT channel_id, message_ts FROM delivered_cards").fetchall()
            assert {(row["channel_id"], row["message_ts"]) for row in bindings} == {
                ("C1", "1789207201.000001"), ("D1", "1789207202.000001"),
            }

    adapter.start = MagicMock(side_effect=run)
    with patch("promise_keeper.__main__.load_dotenv"), patch("promise_keeper.__main__.load_settings", return_value=settings), \
         patch("promise_keeper.__main__.OpenAI"), patch("promise_keeper.__main__.datetime") as clock, \
         patch("promise_keeper.__main__.run_agent", side_effect=lambda event, database, *_: process_event(event.model_copy(update={"received_at": now}), database, lambda *_: decision)), \
         patch("promise_keeper.adapters.slack.UserAction", side_effect=lambda **fields: UserAction(received_at=now, **fields)), \
         patch("promise_keeper.adapters.slack.SlackAdapter", side_effect=wire):
        clock.now.return_value = now
        main([])
