from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from promise_keeper.models import AgentDecision, ContextMessage, NormalizedEvent, PipelineResult, UserAction
from promise_keeper.pipeline import handle_user_action, process_event
from promise_keeper.reminders import check_reminders
from promise_keeper.storage import bind_card, get_promise, initialize_storage, pending_responses, record_delivery


@pytest.fixture
def database(tmp_path: Path):
    connection = initialize_storage(str(tmp_path / "synthetic.db"))
    yield connection
    connection.close()


@pytest.fixture
def event() -> NormalizedEvent:
    now = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    return NormalizedEvent(event_id="message-1", workspace_id="T1", channel_id="C1", author_id="alice",
                           text="I'll send the designs by noon today", event_ts=f"{int(now.timestamp())}.000001", received_at=now)


@pytest.fixture
def interpret(event: NormalizedEvent):
    return MagicMock(return_value=AgentDecision(operation="create", action="Send the designs", evidence="I'll send the designs",
                                               deadline_text="by noon today", deadline_at=event.received_at.replace(hour=12)))


@pytest.fixture
def promise_id(database, event, interpret) -> str:
    result = process_event(event, database, interpret)
    with database:
        bind_card(database, "T1", "C1", "1789207201.000001", result.promise_card.promise_id)
    return result.promise_card.promise_id


@pytest.fixture
def action(event, promise_id) -> UserAction:
    now = event.received_at + timedelta(minutes=1)
    return UserAction(action_name="confirm", promise_id=promise_id, actor_id="alice", workspace_id="T1",
                      event_id="action-1", channel_id="C1", message_ts="1789207201.000001", occurred_at=now, received_at=now)


def test_creation_and_duplicate_event(database, event, interpret) -> None:
    first = process_event(event, database, interpret)
    second = process_event(event, database, interpret)

    assert first.promise_card.status == "pending_confirmation"
    assert first.should_respond
    assert second.status == "duplicate"
    assert not second.should_respond
    interpret.assert_called_once()
    assert database.execute("SELECT COUNT(*) FROM promises").fetchone()[0] == 1
    assert database.execute("SELECT COUNT(*) FROM promise_history").fetchone()[0] == 1


def test_owner_and_lifecycle_checks(database, action) -> None:
    denied = handle_user_action(action.model_copy(update={"actor_id": "bob"}), database)
    assert not denied.success
    assert "Only the promise owner" in denied.error_message
    pending_complete = action.model_copy(update={"event_id": "pending-complete", "action_name": "complete"})
    assert not handle_user_action(pending_complete, database).success
    confirm = action.model_copy(update={"event_id": "owner-confirm"})
    assert handle_user_action(confirm, database).updated_card.status == "confirmed"
    complete = action.model_copy(update={"event_id": "owner-complete", "action_name": "complete", "occurred_at": action.occurred_at + timedelta(minutes=1)})
    assert handle_user_action(complete, database).updated_card.status == "completed"
    reopen = complete.model_copy(update={"event_id": "owner-reopen", "action_name": "confirm"})
    assert not handle_user_action(reopen, database).success
    assert get_promise(database, action.promise_id).status == "completed"


def test_restart_preserves_owner_and_agreement(tmp_path, event, interpret) -> None:
    path = str(tmp_path / "restart.db")
    database = initialize_storage(path)
    result = process_event(event, database, interpret)
    with database:
        bind_card(database, "T1", "C1", "1789207201.000001", result.promise_card.promise_id)
    database.close()
    database = initialize_storage(path)
    try:
        action = UserAction(action_name="confirm", promise_id=result.promise_card.promise_id, actor_id="bob",
                            workspace_id="T1", event_id="restart-click", channel_id="C1", message_ts="1789207201.000001",
                            occurred_at=event.received_at + timedelta(minutes=1))
        assert not handle_user_action(action, database).success
        stored = get_promise(database, result.promise_card.promise_id)
        assert stored.owner_id == "alice"
        assert stored.action == "Send the designs"
    finally:
        database.close()


def test_unknown_promise_is_not_created(database, action) -> None:
    result = handle_user_action(action.model_copy(update={"promise_id": "unknown"}), database)
    assert not result.success
    assert get_promise(database, "unknown") is None


@pytest.mark.parametrize("updates", [{"workspace_id": "T2"}, {"channel_id": "C2"}, {"message_ts": "1789207202.000001"}])
def test_controls_cannot_cross_scope(database, action, updates) -> None:
    assert not handle_user_action(action.model_copy(update=updates), database).success


def test_verified_private_card_accepts_owner_action(database, action) -> None:
    with database:
        bind_card(database, "T1", "D1", "1789207202.000001", action.promise_id)
    private = action.model_copy(update={"channel_id": "D1", "message_ts": "1789207202.000001"})
    assert handle_user_action(private, database).success


def test_duplicate_action_writes_history_once(database, action) -> None:
    assert handle_user_action(action, database).success
    assert handle_user_action(action, database).success
    assert database.execute("SELECT COUNT(*) FROM promise_history WHERE operation = 'confirm'").fetchone()[0] == 1


def test_old_action_cannot_overwrite_newer_agreement(database, action) -> None:
    handle_user_action(action, database)
    newer = action.model_copy(update={"action_name": "reschedule", "event_id": "new-deadline",
                                     "occurred_at": action.occurred_at + timedelta(hours=2),
                                     "deadline_at": action.received_at + timedelta(days=1), "deadline_text": "tomorrow"})
    assert handle_user_action(newer, database).success
    older = newer.model_copy(update={"event_id": "late-deadline", "occurred_at": action.occurred_at + timedelta(hours=1),
                                     "deadline_at": action.received_at + timedelta(days=2)})
    assert not handle_user_action(older, database).success
    assert get_promise(database, action.promise_id).deadline_at == newer.deadline_at
    history = database.execute("SELECT previous_json, current_json FROM promise_history WHERE operation = 'reschedule'").fetchone()
    assert 'by noon today' in history["previous_json"]
    assert 'tomorrow' in history["current_json"]


def test_snooze_and_one_private_reminder(database, action, event) -> None:
    handle_user_action(action, database)
    now = event.received_at + timedelta(hours=3)
    send = MagicMock(return_value=True)
    assert check_reminders(database, send, now, "T1", ("C1",)) == 1
    assert check_reminders(database, send, now + timedelta(minutes=1), "T1", ("C1",)) == 0
    snooze = action.model_copy(update={"action_name": "snooze", "event_id": "snooze", "occurred_at": now, "received_at": now})
    deadline = get_promise(database, action.promise_id).deadline_at
    assert handle_user_action(snooze, database).success
    assert get_promise(database, action.promise_id).deadline_at == deadline
    assert get_promise(database, action.promise_id).snoozed_until == now + timedelta(hours=1)
    assert check_reminders(database, send, now + timedelta(minutes=59), "T1", ("C1",)) == 0
    assert check_reminders(database, send, now + timedelta(hours=1), "T1", ("C1",)) == 1
    assert send.call_args.args[0].owner_id == "alice"
    completed = snooze.model_copy(update={"action_name": "complete", "event_id": "complete", "occurred_at": now + timedelta(hours=2)})
    assert handle_user_action(completed, database).success
    assert check_reminders(database, send, now + timedelta(days=1), "T1", ("C1",)) == 0


def test_failed_reminder_retries_are_bounded_and_persistent(database, action, event) -> None:
    handle_user_action(action, database)
    now = event.received_at + timedelta(hours=3)
    send = MagicMock(return_value=False)
    assert check_reminders(database, send, now, "T1", ("C1",)) == 0
    assert check_reminders(database, send, now + timedelta(seconds=29), "T1", ("C1",)) == 0
    check_reminders(database, send, now + timedelta(seconds=30), "T1", ("C1",))
    check_reminders(database, send, now + timedelta(seconds=90), "T1", ("C1",))
    check_reminders(database, send, now + timedelta(days=1), "T1", ("C1",))
    assert send.call_count == 3
    assert get_promise(database, action.promise_id).reminder_sent_at is None


@pytest.mark.parametrize("state", ["pending_confirmation", "dismissed", "completed"])
def test_closed_or_unconfirmed_promises_do_not_remind(database, promise_id, event, state) -> None:
    from promise_keeper.models import PromiseRecord
    from promise_keeper.storage import save_promise

    promise = get_promise(database, promise_id)
    with database:
        save_promise(database, PromiseRecord.model_validate({**promise.model_dump(), "status": state}))
    send = MagicMock(return_value=True)
    assert check_reminders(database, send, event.received_at + timedelta(days=1), "T1", ("C1",)) == 0
    send.assert_not_called()


def test_missing_deadline_and_disabled_scope_do_not_remind(database, action, event) -> None:
    from promise_keeper.models import PromiseRecord
    from promise_keeper.storage import save_promise

    handle_user_action(action, database)
    send = MagicMock(return_value=True)
    assert check_reminders(database, send, event.received_at + timedelta(days=1), "T2", ("C1",)) == 0
    assert check_reminders(database, send, event.received_at + timedelta(days=1), "T1", ("C2",)) == 0
    promise = get_promise(database, action.promise_id)
    with database:
        save_promise(database, PromiseRecord.model_validate({**promise.model_dump(), "deadline_at": None, "deadline_text": None}))
    assert check_reminders(database, send, event.received_at + timedelta(days=1), "T1", ("C1",)) == 0
    send.assert_not_called()


@pytest.mark.parametrize("decision", [
    {"operation": "create", "action": "Send designs", "evidence": "invented"},
    {"operation": "create", "action": "Send designs", "evidence": "I'll send", "deadline_text": "tomorrow", "deadline_at": "2026-09-13T12:00:00Z"},
    {"operation": "complete", "promise_id": "out-of-scope", "evidence": "I'll send"},
    {"operation": "create", "action": "Send designs", "evidence": "I'll send", "owner_id": "bob"},
])
def test_invalid_or_ungrounded_model_output_cannot_write(database, event, decision) -> None:
    result = process_event(event, database, lambda source, promises: decision)
    assert result.status == "failed"
    assert database.execute("SELECT COUNT(*) FROM promises").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0] == 0


def test_model_failure_does_not_suppress_recovery(database, event, interpret) -> None:
    failed = MagicMock(side_effect=TimeoutError)
    assert process_event(event, database, failed).status == "failed"
    assert process_event(event, database, interpret).promise_card is not None


def test_delivery_failure_does_not_create_a_new_promise(database, event, interpret) -> None:
    result = process_event(event, database, interpret)
    record_delivery(database, "T1", event.event_id, "message", None, event.received_at)
    assert process_event(event, database, interpret).status == "duplicate"
    assert not pending_responses(database, "T1", event.received_at + timedelta(seconds=29))
    pending = pending_responses(database, "T1", event.received_at + timedelta(seconds=30))
    assert len(pending) == 1
    assert PipelineResult.model_validate_json(pending[0]["result_json"]).promise_card.promise_id == result.promise_card.promise_id
    record_delivery(database, "T1", event.event_id, "message", "1789207201.000001", event.received_at + timedelta(seconds=30))
    assert not pending_responses(database, "T1", event.received_at + timedelta(days=1))


def test_contextual_acceptance_uses_same_core(database, event) -> None:
    contextual = event.model_copy(update={"text": "Yes, by noon today.", "context_messages": (
        ContextMessage(user_id="bob", text="Can you send the designs?", ts=f"{int(event.received_at.timestamp()) - 1}.000001"),
    )})
    interpret = MagicMock(return_value=AgentDecision(operation="create", action="Send designs", evidence="Yes",
                                                   deadline_text="by noon today", deadline_at=event.received_at.replace(hour=12)))
    result = process_event(contextual, database, interpret)
    assert result.promise_card.owner_id == "alice"
    assert interpret.call_args.args[0].context_messages == contextual.context_messages


def test_natural_completion_uses_owner_rules(database, action, event) -> None:
    handle_user_action(action, database)
    now = event.received_at + timedelta(hours=1)
    done = event.model_copy(update={"event_id": "done-message", "text": "Sent the designs.", "event_ts": f"{int(now.timestamp())}.000001", "received_at": now})
    decision = AgentDecision(operation="complete", promise_id=action.promise_id, evidence="Sent the designs")
    result = process_event(done, database, lambda source, promises: decision)
    assert result.promise_card.status == "completed"


def test_transaction_rolls_back_partial_tool_failure(database, event, interpret, monkeypatch) -> None:
    def failed_history(*args):
        raise RuntimeError("synthetic write failure")

    monkeypatch.setattr("promise_keeper.tools.record_history", failed_history)
    with pytest.raises(RuntimeError):
        process_event(event, database, interpret)
    assert database.execute("SELECT COUNT(*) FROM promises").fetchone()[0] == 0
    assert database.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0] == 0


@pytest.mark.parametrize("updates", [{"author_id": ""}, {"text": " "}, {"received_at": datetime(2026, 9, 12)}, {"event_ts": "garbage"}])
def test_event_rejects_invalid_boundary_data(event, updates) -> None:
    with pytest.raises(ValidationError):
        NormalizedEvent.model_validate({**event.model_dump(), **updates})


def test_pipeline_response_flag_cannot_disagree() -> None:
    result = PipelineResult(thread_reply_text="Please clarify.")
    assert result.should_respond
    assert PipelineResult.model_validate_json(result.model_dump_json()) == result
    with pytest.raises(ValidationError):
        PipelineResult(should_respond=False, thread_reply_text="Please clarify.")
def test_concurrent_duplicate_events_commit_once(tmp_path, event, interpret) -> None:
    path = str(tmp_path / "concurrent.db")
    with closing(initialize_storage(path)):
        pass
    barrier = Barrier(2)

    def run():
        with closing(initialize_storage(path)) as database:
            def decide(source, promises):
                barrier.wait(timeout=5)
                return interpret.return_value

            return process_event(event, database, decide)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda index: run(), range(2)))
    assert sorted(result.status for result in results) == ["duplicate", "processed"]
    with closing(initialize_storage(path)) as database:
        assert database.execute("SELECT COUNT(*) FROM promises").fetchone()[0] == 1
        assert database.execute("SELECT COUNT(*) FROM promise_history").fetchone()[0] == 1


def test_reminder_acknowledgement_survives_restart(tmp_path, event, interpret) -> None:
    path = str(tmp_path / "reminder-restart.db")
    with closing(initialize_storage(path)) as database:
        promise_id = process_event(event, database, interpret).promise_card.promise_id
        with database:
            bind_card(database, "T1", "C1", "1789207201.000001", promise_id)
        action = UserAction(
            action_name="confirm", promise_id=promise_id, actor_id="alice", workspace_id="T1",
            event_id="confirm", channel_id="C1", message_ts="1789207201.000001",
            occurred_at=event.received_at + timedelta(minutes=1), received_at=event.received_at + timedelta(minutes=1),
        )
        handle_user_action(action, database)
        assert check_reminders(database, lambda notification: True, event.received_at + timedelta(hours=3), "T1", ("C1",)) == 1
    with closing(initialize_storage(path)) as database:
        send = MagicMock(return_value=True)
        assert check_reminders(database, send, event.received_at + timedelta(days=1), "T1", ("C1",)) == 0
        send.assert_not_called()


def test_replayed_action_cannot_change_card_destination(database, action) -> None:
    handle_user_action(action, database)
    replay = action.model_copy(update={"message_ts": "1789207202.000001"})
    assert not handle_user_action(replay, database).success
