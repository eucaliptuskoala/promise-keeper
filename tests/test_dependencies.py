"""Tests for promise dependencies, cycle detection and unblocking workflows."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from promise_keeper.adapters.slack import build_promise_card
from promise_keeper.models import AgentDecision, NormalizedEvent, PromiseCardData, UserAction
from promise_keeper.pipeline import handle_user_action, process_event
from promise_keeper.reminders import check_reminders
from promise_keeper.storage import (
    get_promise,
    has_dependency_cycle,
    initialize_storage,
    list_dependent_promises,
    list_thread_open_promises,
    bind_card,
)
from promise_keeper.tools import create_promise, execute_tool


@pytest.fixture
def database(tmp_path: Path):
    connection = initialize_storage(str(tmp_path / "test_dep.db"))
    yield connection
    connection.close()


def test_has_dependency_cycle(database):
    now = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    ev1 = NormalizedEvent(
        event_id="e1", workspace_id="T1", channel_id="C1", author_id="alice",
        text="Task 1", event_ts="100.000001", thread_ts="100.000001", received_at=now,
    )
    p1 = create_promise(database, ev1, AgentDecision(operation="create", action="Task 1", evidence="Task 1"))
    ev2 = NormalizedEvent(
        event_id="e2", workspace_id="T1", channel_id="C1", author_id="bob",
        text="Task 2", event_ts="101.000001", thread_ts="100.000001", received_at=now,
    )
    p2 = create_promise(
        database, ev2,
        AgentDecision(operation="create", action="Task 2", evidence="Task 2", depends_on_promise_id=p1.promise_id),
    )

    # Self-dependency is a cycle
    assert has_dependency_cycle(database, p1.promise_id, p1.promise_id) is True

    # P2 depends on P1, so making P1 depend on P2 is a cycle
    assert has_dependency_cycle(database, p1.promise_id, p2.promise_id) is True

    # A new promise P3 depending on P2 is not a cycle
    assert has_dependency_cycle(database, "p-new", p2.promise_id) is False


def test_dependency_lifecycle_and_unblocking(database):
    now = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)

    # 1. Alice creates Prerequisite P1
    ev1 = NormalizedEvent(
        event_id="e1", workspace_id="T1", channel_id="C1", author_id="alice",
        text="I will build the API by noon", event_ts="100.000001", thread_ts="100.000001", received_at=now,
    )
    p1 = create_promise(
        database, ev1,
        AgentDecision(operation="create", action="Build API", evidence="I will build the API", deadline_text="noon"),
    )

    # Alice confirms P1
    action_confirm_p1 = UserAction(
        action_name="confirm", promise_id=p1.promise_id, actor_id="alice", workspace_id="T1",
        event_id="c1", channel_id="C1", message_ts="100.000001", occurred_at=now,
    )
    res_c1 = execute_tool(database, action_confirm_p1)
    assert res_c1.success is True
    assert res_c1.updated_card.status == "confirmed"

    # 2. Bob creates Dependent P2, relative deadline 2 days (172800 seconds)
    ev2 = NormalizedEvent(
        event_id="e2", workspace_id="T1", channel_id="C1", author_id="bob",
        text="I will integrate the frontend within 2 days after that",
        event_ts="101.000001", thread_ts="100.000001", received_at=now,
    )
    p2 = create_promise(
        database, ev2,
        AgentDecision(
            operation="create", action="Integrate frontend", evidence="I will integrate the frontend",
            depends_on_promise_id=p1.promise_id, relative_deadline_seconds=172800,
        ),
    )
    assert p2.depends_on_promise_id == p1.promise_id
    assert p2.depends_on_action == "Build API"
    assert p2.status == "pending_confirmation"

    # 3. Bob confirms P2 -> enters 'waiting' status because P1 is not completed
    action_confirm_p2 = UserAction(
        action_name="confirm", promise_id=p2.promise_id, actor_id="bob", workspace_id="T1",
        event_id="c2", channel_id="C1", message_ts="101.000001", occurred_at=now,
    )
    res_c2 = execute_tool(database, action_confirm_p2)
    assert res_c2.success is True
    assert res_c2.updated_card.status == "waiting"

    stored_p2 = get_promise(database, p2.promise_id)
    assert stored_p2.status == "waiting"

    # 4. Check reminders: waiting promise should NOT trigger overdue reminders
    sent_reminders = check_reminders(
        database, MagicMock(return_value=True), now + timedelta(days=5), "T1", ("C1",),
    )
    # Only P1 has no deadline_at in this setup, so 0 reminders sent; P2 in waiting status is skipped
    assert sent_reminders == 0

    # The delivered dependent card must be surfaced for a transport update on unblock.
    with database:
        bind_card(database, "T1", "C1", "100.000001", p1.promise_id)
        bind_card(database, "T1", "C1", "101.000001", p2.promise_id)

    # 5. Alice completes P1 -> unblocks P2!
    action_complete_p1 = UserAction(
        action_name="complete", promise_id=p1.promise_id, actor_id="alice", workspace_id="T1",
        event_id="comp1", channel_id="C1", message_ts="100.000001", occurred_at=now + timedelta(hours=1),
    )
    res_comp1 = handle_user_action(database=database, action=action_complete_p1)
    assert res_comp1.success is True
    assert res_comp1.updated_card.status == "completed"

    # Verify P2 was returned in unblocked_cards
    assert len(res_comp1.unblocked_cards) == 1
    unblocked_p2 = res_comp1.unblocked_cards[0]
    assert unblocked_p2.promise_id == p2.promise_id
    assert unblocked_p2.status == "confirmed"
    assert unblocked_p2.deadline_at is not None
    assert unblocked_p2.deadline_text == "Within 2 days"

    # Verify database state for P2 is now confirmed
    stored_p2_after = get_promise(database, p2.promise_id)
    assert stored_p2_after.status == "confirmed"
    assert stored_p2_after.deadline_at is not None
    assert len(res_comp1.unblocked_card_updates) == 1
    assert res_comp1.unblocked_card_updates[0].promise_card.promise_id == p2.promise_id
    assert res_comp1.unblocked_card_updates[0].message_ts == "101.000001"


def test_waiting_dependent_cannot_complete_before_prerequisite(database):
    now = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    prerequisite_event = NormalizedEvent(
        event_id="e1", workspace_id="T1", channel_id="C1", author_id="alice",
        text="Build API", event_ts="100.000001", thread_ts="100.000001", received_at=now,
    )
    prerequisite = create_promise(
        database, prerequisite_event,
        AgentDecision(operation="create", action="Build API", evidence="Build API"),
    )
    dependent_event = prerequisite_event.model_copy(update={
        "event_id": "e2", "author_id": "bob", "text": "Build UI", "event_ts": "101.000001",
    })
    dependent = create_promise(
        database, dependent_event,
        AgentDecision(
            operation="create", action="Build UI", evidence="Build UI",
            depends_on_promise_id=prerequisite.promise_id,
        ),
    )
    confirm = UserAction(
        action_name="confirm", promise_id=dependent.promise_id, actor_id="bob", workspace_id="T1",
        event_id="confirm", channel_id="C1", message_ts="101.000001", occurred_at=now,
    )
    assert execute_tool(database, confirm).updated_card.status == "waiting"

    result = execute_tool(database, confirm.model_copy(update={"action_name": "complete", "event_id": "complete"}))

    assert not result.success
    assert get_promise(database, dependent.promise_id).status == "waiting"


def test_prerequisite_with_open_dependents_cannot_be_dismissed(database):
    now = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    event = NormalizedEvent(
        event_id="e1", workspace_id="T1", channel_id="C1", author_id="alice",
        text="Build API", event_ts="100.000001", thread_ts="100.000001", received_at=now,
    )
    prerequisite = create_promise(database, event, AgentDecision(operation="create", action="Build API", evidence="Build API"))
    dependent = create_promise(
        database, event.model_copy(update={"event_id": "e2", "author_id": "bob", "text": "Build UI", "event_ts": "101.000001"}),
        AgentDecision(operation="create", action="Build UI", evidence="Build UI", depends_on_promise_id=prerequisite.promise_id),
    )
    result = execute_tool(
        database,
        UserAction(
            action_name="dismiss", promise_id=prerequisite.promise_id, actor_id="alice", workspace_id="T1",
            event_id="dismiss", channel_id="C1", message_ts="100.000001", occurred_at=now,
        ),
    )

    assert not result.success
    assert get_promise(database, prerequisite.promise_id).status == "pending_confirmation"
    assert get_promise(database, dependent.promise_id).status == "pending_confirmation"


def test_relative_deadline_uses_completion_event_time(database):
    completed_at = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    received_at = completed_at + timedelta(hours=4)
    event = NormalizedEvent(
        event_id="e1", workspace_id="T1", channel_id="C1", author_id="alice",
        text="Build API", event_ts="100.000001", thread_ts="100.000001", received_at=completed_at,
    )
    prerequisite = create_promise(database, event, AgentDecision(operation="create", action="Build API", evidence="Build API"))
    execute_tool(database, UserAction(
        action_name="confirm", promise_id=prerequisite.promise_id, actor_id="alice", workspace_id="T1",
        event_id="confirm-prerequisite", channel_id="C1", message_ts="100.000001", occurred_at=completed_at,
    ))
    dependent = create_promise(
        database, event.model_copy(update={"event_id": "e2", "author_id": "bob", "text": "Build UI", "event_ts": "101.000001"}),
        AgentDecision(
            operation="create", action="Build UI", evidence="Build UI", depends_on_promise_id=prerequisite.promise_id,
            relative_deadline_seconds=172800,
        ),
    )
    execute_tool(database, UserAction(
        action_name="confirm", promise_id=dependent.promise_id, actor_id="bob", workspace_id="T1",
        event_id="confirm-dependent", channel_id="C1", message_ts="101.000001", occurred_at=completed_at,
    ))

    execute_tool(database, UserAction(
        action_name="complete", promise_id=prerequisite.promise_id, actor_id="alice", workspace_id="T1",
        event_id="complete-prerequisite", channel_id="C1", message_ts="100.000001", occurred_at=completed_at,
        received_at=received_at,
    ))

    assert get_promise(database, dependent.promise_id).deadline_at == completed_at + timedelta(days=2)


def test_confirm_when_prerequisite_already_completed(database):
    now = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    ev1 = NormalizedEvent(
        event_id="e1", workspace_id="T1", channel_id="C1", author_id="alice",
        text="Build API", event_ts="100.000001", thread_ts="100.000001", received_at=now,
    )
    p1 = create_promise(database, ev1, AgentDecision(operation="create", action="Build API", evidence="Build API"))
    execute_tool(
        database,
        UserAction(
            action_name="confirm", promise_id=p1.promise_id, actor_id="alice", workspace_id="T1",
            event_id="c1", channel_id="C1", message_ts="100.000001", occurred_at=now,
        ),
    )
    execute_tool(
        database,
        UserAction(
            action_name="complete", promise_id=p1.promise_id, actor_id="alice", workspace_id="T1",
            event_id="comp1", channel_id="C1", message_ts="100.000001", occurred_at=now + timedelta(hours=1),
        ),
    )

    # Bob creates P2 depending on already completed P1
    ev2 = NormalizedEvent(
        event_id="e2", workspace_id="T1", channel_id="C1", author_id="bob",
        text="Frontend", event_ts="102.000001", thread_ts="100.000001", received_at=now + timedelta(hours=2),
    )
    p2 = create_promise(
        database, ev2,
        AgentDecision(
            operation="create", action="Frontend", evidence="Frontend", depends_on_promise_id=p1.promise_id,
        ),
    )
    # Confirming goes straight to confirmed because prerequisite is completed
    res = execute_tool(
        database,
        UserAction(
            action_name="confirm", promise_id=p2.promise_id, actor_id="bob", workspace_id="T1",
            event_id="c2", channel_id="C1", message_ts="102.000001", occurred_at=now + timedelta(hours=2),
        ),
    )
    assert res.success is True
    assert res.updated_card.status == "confirmed"


def test_pipeline_dependency_validation(database):
    now = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    ev = NormalizedEvent(
        event_id="e1", workspace_id="T1", channel_id="C1", author_id="bob",
        text="I will write docs after nonexistent task", event_ts="100.000001", received_at=now,
    )
    # Model returns depends_on_promise_id that doesn't exist in thread
    interpret_bad = MagicMock(
        return_value=AgentDecision(
            operation="create", action="Write docs", evidence="I will write docs",
            depends_on_promise_id="p-nonexistent",
        ),
    )
    res = process_event(ev, database, interpret_bad)
    assert res.status == "failed"
    assert res.error_code == "interpretation_failed"


def test_build_promise_card_waiting_state():
    card = PromiseCardData(
        promise_id="p-123",
        owner_id="U123",
        action="Deploy app",
        status="waiting",
        depends_on_promise_id="p-prereq",
        depends_on_action="Build backend API",
        deadline_text="TBD",
    )
    blocks = build_promise_card(card)
    text = blocks[0]["text"]["text"]
    assert "Waiting on prerequisite" in text
    assert "Prerequisite:* Build backend API" in text

    # Check button elements on waiting card
    action_block = next(b for b in blocks if b["type"] == "actions")
    action_ids = [el["action_id"] for el in action_block["elements"]]
    assert "promise_complete" not in action_ids
    assert "promise_reschedule" in action_ids
    assert "promise_dismiss" in action_ids
