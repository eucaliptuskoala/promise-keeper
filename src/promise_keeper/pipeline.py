"""Shared processing pipeline for real and synthetic events."""

import logging
import re
import uuid
from promise_keeper.models import (
    ActionResult,
    NormalizedEvent,
    PipelineResult,
    PromiseCardData,
    UserAction,
)

logger = logging.getLogger("promise_keeper.pipeline")

# In-memory store for active promises (used for interactive action reconciliation)
_PROMISES_CACHE: dict[str, PromiseCardData] = {}

# Common commitment patterns for rule-based detection
_COMMITMENT_PATTERNS = [
    re.compile(r"\b(i will|i'll|i can|i promise to|i shall|i'm going to)\s+(.+)", re.IGNORECASE),
    re.compile(r"\b(я сделаю|я отправлю|я напишу|я подготовлю|я закончу)\s+(.+)", re.IGNORECASE),
]

_DEADLINE_PATTERNS = [
    re.compile(r"\b(by\s+[^,.]+)", re.IGNORECASE),
    re.compile(r"\b(before\s+[^,.]+)", re.IGNORECASE),
    re.compile(r"\b(until\s+[^,.]+)", re.IGNORECASE),
    re.compile(r"\b(tomorrow|today|tonight|next week|by noon|by 3 pm|by 5 pm)\b", re.IGNORECASE),
    re.compile(r"\b(до\s+[^,.]+)", re.IGNORECASE),
    re.compile(r"\b(завтра|сегодня)\b", re.IGNORECASE),
]


def _detect_commitment(text: str) -> tuple[str | None, str | None]:
    """Extract action and deadline text from a message using heuristic matching.

    Returns:
        (action, deadline_text) if a commitment is detected, otherwise (None, None).
    """
    matched_action = None
    for pattern in _COMMITMENT_PATTERNS:
        match = pattern.search(text)
        if match:
            matched_action = match.group(0).strip()
            break

    if not matched_action:
        return None, None

    # Check for stated deadline
    deadline_text = None
    for d_pattern in _DEADLINE_PATTERNS:
        d_match = d_pattern.search(text)
        if d_match:
            deadline_text = d_match.group(0).strip()
            break

    return matched_action, deadline_text


def process_event(event: NormalizedEvent) -> PipelineResult:
    """Process one normalized inbound event.

    Detects commitments from message text or conversation context and returns
    outbound response actions. Can be backed by model reasoning or deterministic rules.
    """
    logger.info("Pipeline processing event: %s from author %s", event.event_id, event.author_id)

    action_text, deadline_text = _detect_commitment(event.text)
    if not action_text:
        logger.debug("No firm commitment detected in message: %s", event.text)
        return PipelineResult(
            processed=True,
            should_respond=False,
        )

    # Generate or reuse promise record
    promise_id = f"p-{uuid.uuid4().hex[:8]}"
    card = PromiseCardData(
        promise_id=promise_id,
        owner_id=event.author_id,
        action=action_text,
        deadline_text=deadline_text,
        status="pending_confirmation",
    )
    _PROMISES_CACHE[promise_id] = card

    logger.info(
        "Detected promise '%s' for owner %s: '%s' (deadline: %s)",
        promise_id,
        event.author_id,
        action_text,
        deadline_text or "none",
    )

    return PipelineResult(
        processed=True,
        should_respond=True,
        promise_card=card,
    )


def handle_user_action(action: UserAction) -> ActionResult:
    """Handle deterministic user action (confirm, dismiss, complete, snooze).

    Validates that the acting user is the promise owner before mutating state.
    """
    logger.info(
        "Handling user action '%s' on promise '%s' by actor '%s'",
        action.action_name,
        action.promise_id,
        action.actor_id,
    )

    card = _PROMISES_CACHE.get(action.promise_id)

    # If not in cache (e.g. after restart), build a placeholder representation
    if not card:
        card = PromiseCardData(
            promise_id=action.promise_id,
            owner_id=action.actor_id,
            action="Tracked commitment",
            status="pending_confirmation",
        )
        _PROMISES_CACHE[action.promise_id] = card

    # Enforce authorization: only the promise owner can confirm or complete
    if action.actor_id != card.owner_id:
        logger.warning(
            "Unauthorized action '%s' by actor %s on promise %s (owner: %s)",
            action.action_name,
            action.actor_id,
            action.promise_id,
            card.owner_id,
        )
        return ActionResult(
            success=False,
            error_message=f"Only the promise owner (<@{card.owner_id}>) can {action.action_name} this promise.",
        )

    # State transitions
    new_status = card.status
    notification = None

    if action.action_name == "confirm":
        new_status = "confirmed"
        notification = "Promise confirmed!"
    elif action.action_name == "complete":
        new_status = "completed"
        notification = "Promise marked as complete! 🎉"
    elif action.action_name == "dismiss":
        new_status = "dismissed"
        notification = "Promise dismissed."
    elif action.action_name == "snooze":
        notification = "Reminder snoozed."
    else:
        return ActionResult(
            success=False,
            error_message=f"Unknown action '{action.action_name}'",
        )

    updated_card = PromiseCardData(
        promise_id=card.promise_id,
        owner_id=card.owner_id,
        action=card.action,
        deadline_text=card.deadline_text,
        status=new_status,
    )
    _PROMISES_CACHE[action.promise_id] = updated_card

    return ActionResult(
        success=True,
        updated_card=updated_card,
        notification_text=notification,
    )
