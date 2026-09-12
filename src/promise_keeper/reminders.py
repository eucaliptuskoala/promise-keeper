"""Overdue selection, delivery acknowledgement and bounded retries."""

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Callable

from promise_keeper.models import PromiseRecord, ReminderNotification
from promise_keeper.storage import get_promise, save_promise

logger = logging.getLogger("promise_keeper.reminders")


def check_reminders(
    database: sqlite3.Connection, send: Callable[[ReminderNotification], bool], now: datetime,
    workspace_id: str, enabled_channels: tuple[str, ...],
) -> int:
    """Caller serializes this check with events and actions through delivery."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("Reminder clock requires a timezone")
    now = now.astimezone(timezone.utc)
    rows = database.execute("SELECT promise_id FROM promises WHERE workspace_id = ?", (workspace_id,)).fetchall()
    sent_count = 0
    for row in rows:
        promise = get_promise(database, row["promise_id"])
        if promise.channel_id not in enabled_channels or promise.status != "confirmed" or promise.deadline_at is None:
            continue
        if promise.deadline_at >= now or promise.reminder_sent_at is not None or promise.reminder_attempts >= 3:
            continue
        if promise.snoozed_until is not None and promise.snoozed_until > now:
            continue
        if promise.reminder_retry_at is not None and promise.reminder_retry_at > now:
            continue
        attempts = promise.reminder_attempts + 1
        attempted = PromiseRecord.model_validate({
            **promise.model_dump(), "reminder_attempts": attempts,
            "reminder_retry_at": now + timedelta(seconds=30 * attempts),
        })
        with database:
            save_promise(database, attempted)
        notification = ReminderNotification(
            promise_id=promise.promise_id, workspace_id=promise.workspace_id, owner_id=promise.owner_id,
            action=promise.action, deadline_text=promise.deadline_text,
            channel_id=promise.channel_id, thread_ts=promise.thread_ts,
        )
        try:
            delivered = send(notification)
        except Exception as error:
            logger.warning("Reminder delivery failed for %s (%s)", promise.promise_id, type(error).__name__)
            delivered = False
        if delivered:
            current = get_promise(database, promise.promise_id)
            if current.last_event_at != attempted.last_event_at or current.snoozed_until != attempted.snoozed_until:
                continue
            with database:
                save_promise(database, PromiseRecord.model_validate({**current.model_dump(), "reminder_sent_at": now}))
            sent_count += 1
    return sent_count
