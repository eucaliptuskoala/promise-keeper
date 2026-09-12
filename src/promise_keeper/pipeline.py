"""Shared event processing with explicit storage and model dependencies."""

import logging
import sqlite3
from typing import Callable

from openai import OpenAIError
from pydantic import ValidationError

from promise_keeper.models import (
    ActionResult,
    AgentDecision,
    CardUpdate,
    NormalizedEvent,
    PipelineResult,
    PromiseRecord,
    UserAction,
)
from promise_keeper.storage import (
    get_processed_event,
    get_promise,
    has_dependency_cycle,
    list_open_promises,
    list_delivered_cards,
    list_thread_open_promises,
    record_processed_event,
)
from promise_keeper.tools import create_promise, execute_tool

logger = logging.getLogger("promise_keeper.pipeline")


def process_event(
    event: NormalizedEvent,
    database: sqlite3.Connection,
    interpret: Callable[..., AgentDecision],
) -> PipelineResult:
    if get_processed_event(database, event.workspace_id, event.event_id, "message"):
        return PipelineResult(status="duplicate")
    promises = list_open_promises(
        database, event.workspace_id, event.channel_id, event.author_id, event.thread_ts or event.event_ts,
    )
    thread_promises = list_thread_open_promises(
        database, event.workspace_id, event.channel_id, event.thread_ts or event.event_ts,
        before=event.occurred_at,
    )
    try:
        try:
            raw_decision = interpret(event, promises, thread_promises=thread_promises)
        except TypeError:
            raw_decision = interpret(event, promises)
        decision = AgentDecision.model_validate(raw_decision)
        if decision.operation in ("create", "complete", "reschedule"):
            if not decision.evidence.strip() or decision.evidence not in event.text:
                raise ValueError("Ungrounded evidence")
        if decision.deadline_text is not None:
            texts = [event.text, *(message.text for message in event.context_messages)]
            if not any(decision.deadline_text in text for text in texts):
                raise ValueError("Ungrounded deadline")
        if decision.promise_id is not None and decision.promise_id not in {promise.promise_id for promise in promises}:
            raise ValueError("Out-of-scope promise")
        if decision.depends_on_promise_id is not None:
            if decision.depends_on_promise_id not in {promise.promise_id for promise in thread_promises}:
                raise ValueError("Out-of-scope dependency")
            if has_dependency_cycle(database, "", decision.depends_on_promise_id):
                raise ValueError("Cyclic dependency")
    except (OpenAIError, ValidationError, ValueError, TimeoutError) as error:
        logger.warning("Interpretation failed for event %s (%s)", event.event_id, type(error).__name__)
        return PipelineResult(status="failed", error_code="interpretation_failed")


    with database:
        database.execute("BEGIN IMMEDIATE")
        if get_processed_event(database, event.workspace_id, event.event_id, "message"):
            return PipelineResult(status="duplicate")
        if decision.operation == "ignore":
            result = PipelineResult(status="ignored")
        elif decision.operation == "clarify":
            result = PipelineResult(thread_reply_text=decision.clarification)
        elif decision.operation == "create":
            result = PipelineResult(promise_card=create_promise(database, event, decision).card())
        else:
            action = UserAction(
                action_name=decision.operation,
                promise_id=decision.promise_id,
                actor_id=event.author_id,
                workspace_id=event.workspace_id,
                event_id=event.event_id,
                channel_id=event.channel_id,
                message_ts=event.event_ts,
                occurred_at=event.occurred_at,
                received_at=event.received_at,
                deadline_at=decision.deadline_at,
                deadline_text=decision.deadline_text,
            )
            action_result = execute_tool(database, action)
            if action_result.success:
                result = PipelineResult(promise_card=action_result.updated_card)
                for card in action_result.unblocked_cards:
                    for location in list_delivered_cards(database, card.promise_id):
                        record_processed_event(
                            database, event.workspace_id, event.event_id,
                            f"card_update:{location['channel_id']}:{location['message_ts']}", event.author_id,
                            location["channel_id"], location["message_ts"], ActionResult(success=True, updated_card=card),
                            event.received_at,
                        )
            else:
                result = PipelineResult(thread_reply_text=action_result.error_message)
        record_processed_event(
            database, event.workspace_id, event.event_id, "message", event.author_id,
            event.channel_id, event.thread_ts or event.event_ts, result, event.received_at,
        )
    return result


def handle_user_action(action: UserAction, database: sqlite3.Connection) -> ActionResult:
    with database:
        database.execute("BEGIN IMMEDIATE")
        processed = get_processed_event(database, action.workspace_id, action.event_id, "action")
        if processed:
            if (
                processed["actor_id"] != action.actor_id
                or processed["channel_id"] != action.channel_id
                or processed["target_ts"] != action.message_ts
            ):
                return ActionResult(
                    success=False, error_message="Action metadata does not match the original event.",
                )
            previous = ActionResult.model_validate_json(processed["result_json"])
            if previous.success:
                if previous.updated_card.promise_id != action.promise_id:
                    return ActionResult(
                        success=False, error_message="Action metadata does not match the original event.",
                    )
                promise = get_promise(database, action.promise_id)
                if promise is None or promise.workspace_id != action.workspace_id or promise.owner_id != action.actor_id:
                    return ActionResult(success=False, error_message="Promise not found.")
                return ActionResult(
                    success=True, updated_card=promise.card(), notification_text="Action already processed.",
                )
            return previous
        result = execute_tool(database, action, require_card=True)
        if result.success and result.unblocked_cards:
            card_updates = tuple(
                CardUpdate(
                    promise_card=card,
                    channel_id=location["channel_id"],
                    message_ts=location["message_ts"],
                )
                for card in result.unblocked_cards
                for location in list_delivered_cards(database, card.promise_id)
            )
            result = result.model_copy(update={"unblocked_card_updates": card_updates})
            for update in card_updates:
                record_processed_event(
                    database, action.workspace_id, action.event_id,
                    f"card_update:{update.channel_id}:{update.message_ts}", action.actor_id,
                    update.channel_id, update.message_ts, ActionResult(success=True, updated_card=update.promise_card),
                    action.received_at,
                )
        record_processed_event(
            database, action.workspace_id, action.event_id, "action", action.actor_id,
            action.channel_id, action.message_ts, result, action.received_at,
        )
    return result
