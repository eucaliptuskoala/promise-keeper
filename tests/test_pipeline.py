"""Unit tests for the processing pipeline and action handling."""

from promise_keeper.models import NormalizedEvent, UserAction
from promise_keeper.pipeline import handle_user_action, process_event


def test_process_event_detects_promise() -> None:
    event = NormalizedEvent(
        event_id="C1:1710000001.000",
        workspace_id="T1",
        channel_id="C1",
        author_id="UALICE",
        text="I'll send the designs by noon",
        event_ts="1710000001.000",
    )
    result = process_event(event)

    assert result.processed is True
    assert result.should_respond is True
    assert result.promise_card is not None
    assert result.promise_card.owner_id == "UALICE"
    assert "send the designs" in result.promise_card.action
    assert result.promise_card.deadline_text == "by noon"
    assert result.promise_card.status == "pending_confirmation"


def test_process_event_ignores_ordinary_chat() -> None:
    event = NormalizedEvent(
        event_id="C1:1710000002.000",
        workspace_id="T1",
        channel_id="C1",
        author_id="UBOB",
        text="Can someone help me review this PR?",
        event_ts="1710000002.000",
    )
    result = process_event(event)

    assert result.processed is True
    assert result.should_respond is False
    assert result.promise_card is None


def test_handle_user_action_lifecycle() -> None:
    # 1. Create a promise via event
    event = NormalizedEvent(
        event_id="C1:1710000003.000",
        workspace_id="T1",
        channel_id="C1",
        author_id="UALICE",
        text="I will fix the bug today",
        event_ts="1710000003.000",
    )
    result = process_event(event)
    promise_id = result.promise_card.promise_id

    # 2. Unauthorized confirm attempt by Bob
    unauth_action = UserAction(
        action_name="confirm",
        promise_id=promise_id,
        actor_id="UBOB",
        channel_id="C1",
        message_ts="1710000003.001",
    )
    unauth_res = handle_user_action(unauth_action)
    assert unauth_res.success is False
    assert "Only the promise owner" in unauth_res.error_message

    # 3. Authorized confirm by Alice
    confirm_action = UserAction(
        action_name="confirm",
        promise_id=promise_id,
        actor_id="UALICE",
        channel_id="C1",
        message_ts="1710000003.001",
    )
    confirm_res = handle_user_action(confirm_action)
    assert confirm_res.success is True
    assert confirm_res.updated_card.status == "confirmed"

    # 4. Authorized complete by Alice
    complete_action = UserAction(
        action_name="complete",
        promise_id=promise_id,
        actor_id="UALICE",
        channel_id="C1",
        message_ts="1710000003.001",
    )
    complete_res = handle_user_action(complete_action)
    assert complete_res.success is True
    assert complete_res.updated_card.status == "completed"
