"""Authorized commitment writes and deterministic lifecycle rules."""

import sqlite3
import uuid
from datetime import timedelta, timezone

from promise_keeper.models import ActionResult, AgentDecision, NormalizedEvent, PromiseRecord, UserAction
from promise_keeper.storage import (
    get_promise,
    is_delivered_card,
    list_dependent_promises,
    record_history,
    save_promise,
)


def create_promise(database: sqlite3.Connection, event: NormalizedEvent, decision: AgentDecision) -> PromiseRecord:
    now = event.received_at.astimezone(timezone.utc)
    depends_on_action: str | None = None
    if decision.depends_on_promise_id:
        prerequisite = get_promise(database, decision.depends_on_promise_id)
        if prerequisite:
            depends_on_action = prerequisite.action
    promise = PromiseRecord(
        promise_id=f"p-{uuid.uuid4().hex}",
        workspace_id=event.workspace_id,
        channel_id=event.channel_id,
        thread_ts=event.thread_ts or event.event_ts,
        owner_id=event.author_id,
        action=decision.action,
        deadline_text=decision.deadline_text,
        deadline_at=decision.deadline_at.astimezone(timezone.utc) if decision.deadline_at else None,
        source_message_id=event.event_ts,
        source_occurred_at=event.occurred_at,
        created_at=now,
        updated_at=now,
        last_event_at=event.occurred_at,
        depends_on_promise_id=decision.depends_on_promise_id,
        depends_on_action=depends_on_action,
        relative_deadline_seconds=decision.relative_deadline_seconds,
    )
    save_promise(database, promise)
    record_history(database, None, promise, "create", event.author_id, event.event_id, event.occurred_at, now)
    return promise


def execute_tool(database: sqlite3.Connection, action: UserAction, require_card: bool = False) -> ActionResult:
    """The caller commits this change, history and event receipt together."""
    promise = get_promise(database, action.promise_id)
    if promise is None or promise.workspace_id != action.workspace_id:
        return ActionResult(success=False, error_message="Promise not found.")
    if promise.owner_id != action.actor_id:
        return ActionResult(success=False, error_message="Only the promise owner can perform this action.")
    if (require_card or promise.channel_id != action.channel_id) and not is_delivered_card(
        database, action.workspace_id, action.channel_id, action.message_ts, action.promise_id,
    ):
        return ActionResult(success=False, error_message="This card does not belong to this conversation.")
    if action.occurred_at < promise.last_event_at:
        return ActionResult(success=False, error_message="This action is older than the current agreement.")

    now = action.received_at.astimezone(timezone.utc)
    changes = {"updated_at": now, "last_event_at": action.occurred_at.astimezone(timezone.utc)}
    unblocked_cards = []
    if action.action_name == "confirm":
        if promise.status in ("confirmed", "waiting"):
            return ActionResult(
                success=True, updated_card=promise.card(), notification_text="Promise already confirmed.",
            )
        if promise.status != "pending_confirmation":
            return ActionResult(success=False, error_message="Only a pending promise can be confirmed.")
        if promise.depends_on_promise_id:
            prerequisite = get_promise(database, promise.depends_on_promise_id)
            if prerequisite and prerequisite.status != "completed":
                changes["status"] = "waiting"
                notification = "Promise confirmed. Waiting on prerequisite to complete."
            else:
                changes["status"] = "confirmed"
                notification = "Promise confirmed."
        else:
            changes["status"] = "confirmed"
            notification = "Promise confirmed."
    elif action.action_name == "dismiss":
        if promise.status == "dismissed":
            return ActionResult(
                success=True, updated_card=promise.card(), notification_text="Promise already dismissed.",
            )
        if promise.status not in ("pending_confirmation", "waiting"):
            return ActionResult(success=False, error_message="Only a pending or waiting promise can be dismissed.")
        if any(
            dependent.status in ("pending_confirmation", "waiting", "confirmed")
            for dependent in list_dependent_promises(database, promise.promise_id)
        ):
            return ActionResult(
                success=False,
                error_message="Resolve or dismiss dependent promises before dismissing this prerequisite.",
            )
        changes["status"] = "dismissed"
        notification = "Promise dismissed."
    elif action.action_name == "complete":
        if promise.status == "completed":
            return ActionResult(
                success=True, updated_card=promise.card(), notification_text="Promise already completed.",
            )
        if promise.status != "confirmed":
            return ActionResult(success=False, error_message="Confirm the promise before completing it.")
        changes["status"] = "completed"
        notification = "Promise completed."
        for dependent in list_dependent_promises(database, promise.promise_id):
            if dependent.status == "waiting":
                dep_changes: dict = {"status": "confirmed", "updated_at": now}
                if dependent.relative_deadline_seconds and dependent.deadline_at is None:
                    dep_deadline = action.occurred_at.astimezone(timezone.utc) + timedelta(
                        seconds=dependent.relative_deadline_seconds,
                    )
                    dep_changes["deadline_at"] = dep_deadline
                    days = dependent.relative_deadline_seconds // 86400
                    hours = dependent.relative_deadline_seconds // 3600
                    dep_changes["deadline_text"] = f"Within {days} days" if dependent.relative_deadline_seconds % 86400 == 0 else f"Within {hours} hours"
                updated_dep = PromiseRecord.model_validate({**dependent.model_dump(), **dep_changes})
                save_promise(database, updated_dep)
                record_history(
                    database, dependent, updated_dep, "unblock", action.actor_id, action.event_id, action.occurred_at, now,
                )
                unblocked_cards.append(updated_dep.card())
    else:
        if promise.status not in ("confirmed", "waiting"):
            return ActionResult(success=False, error_message="Only confirmed promises can be rescheduled or snoozed.")
        if action.action_name == "reschedule":
            changes.update(
                deadline_at=action.deadline_at.astimezone(timezone.utc),
                deadline_text=action.deadline_text or action.deadline_at.isoformat(),
                snoozed_until=None,
                reminder_sent_at=None,
                reminder_attempts=0,
                reminder_retry_at=None,
            )
            notification = "Deadline updated."
        else:
            if promise.status != "confirmed" or promise.deadline_at is None or promise.deadline_at >= now:
                return ActionResult(success=False, error_message="Only an overdue promise can be snoozed.")
            remind_at = action.remind_at or now + timedelta(hours=1)
            if remind_at <= now:
                return ActionResult(success=False, error_message="Snooze time must be in the future.")
            changes.update(
                snoozed_until=remind_at.astimezone(timezone.utc),
                reminder_sent_at=None,
                reminder_attempts=0,
                reminder_retry_at=None,
            )
            notification = "Reminder snoozed."

    updated = PromiseRecord.model_validate({**promise.model_dump(), **changes})
    save_promise(database, updated)
    record_history(
        database, promise, updated, action.action_name, action.actor_id, action.event_id, action.occurred_at, now,
    )
    return ActionResult(
        success=True,
        updated_card=updated.card(),
        notification_text=notification,
        unblocked_cards=tuple(unblocked_cards),
    )
