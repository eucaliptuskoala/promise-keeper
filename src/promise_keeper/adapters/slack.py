"""Slack Bolt transport adapter with Socket Mode."""

import logging
import re
from typing import Any, Callable

from slack_bolt import App
try:
    from slack_bolt.adapter.socket_mode.websocket_client import SocketModeHandler
except ImportError:
    from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

from promise_keeper.config import Settings, load_settings
from promise_keeper.models import (
    ActionResult,
    ContextMessage,
    NormalizedEvent,
    PipelineResult,
    PromiseCardData,
    ReminderNotification,
    UserAction,
)
from promise_keeper.pipeline import handle_user_action, process_event

logger = logging.getLogger("promise_keeper.adapters.slack")


def build_promise_card(card: PromiseCardData) -> list[dict[str, Any]]:
    """Build Slack Block Kit representation of a promise card."""
    status_display = {
        "pending_confirmation": "Pending Confirmation :hourglass_flowing_sand:",
        "confirmed": "Confirmed :white_check_mark:",
        "completed": "Completed :tada:",
        "dismissed": "Dismissed :heavy_multiplication_x:",
    }.get(card.status, card.status)

    deadline_display = card.deadline_text if card.deadline_text else "Not specified"

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Promise Detected* :handshake:\n"
                    f"*Owner:* <@{card.owner_id}>\n"
                    f"*Action:* {card.action}\n"
                    f"*Deadline:* {deadline_display}\n"
                    f"*Status:* {status_display}"
                ),
            },
        }
    ]

    elements: list[dict[str, Any]] = []
    if card.status == "pending_confirmation":
        elements.extend(
            [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Confirm"},
                    "style": "primary",
                    "action_id": "promise_confirm",
                    "value": card.promise_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Dismiss"},
                    "style": "danger",
                    "action_id": "promise_dismiss",
                    "value": card.promise_id,
                },
            ]
        )
    elif card.status == "confirmed":
        elements.extend(
            [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Mark Done"},
                    "style": "primary",
                    "action_id": "promise_complete",
                    "value": card.promise_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Snooze"},
                    "action_id": "promise_snooze",
                    "value": card.promise_id,
                },
            ]
        )

    if elements:
        blocks.append({"type": "actions", "elements": elements})

    return blocks


def build_reminder_card(notification: ReminderNotification) -> list[dict[str, Any]]:
    """Build Slack Block Kit representation for an overdue private reminder."""
    deadline_display = notification.deadline_text if notification.deadline_text else "Overdue"

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Reminder: Overdue Commitment* :alarm_clock:\n\n"
                    f"You promised: *{notification.action}*\n"
                    f"*Deadline:* {deadline_display}\n\n"
                    f"Have you completed this delivery?"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Mark Done"},
                    "style": "primary",
                    "action_id": "promise_complete",
                    "value": notification.promise_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Snooze"},
                    "action_id": "promise_snooze",
                    "value": notification.promise_id,
                },
            ],
        },
    ]
    return blocks


class SlackAdapter:
    """Slack transport adapter managing Socket Mode, normalization, and outbound delivery."""

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        process_event_fn: Callable[[NormalizedEvent], PipelineResult] = process_event,
        handle_action_fn: Callable[[UserAction], ActionResult] = handle_user_action,
    ) -> None:
        self.bot_token = bot_token
        self.app_token = app_token
        self.process_event_fn = process_event_fn
        self.handle_action_fn = handle_action_fn

        self.app = App(token=self.bot_token)
        auth = self.app.client.auth_test()
        self.bot_user_id: str = auth.get("user_id", "")
        self.workspace_id: str = auth.get("team_id", "")
        self.workspace_name: str = auth.get("team", "")

        logger.info(
            "Initialized Slack adapter for workspace '%s' (bot user: %s)",
            self.workspace_name,
            self.bot_user_id,
        )

        self._register_handlers()
        self.handler = SocketModeHandler(self.app, self.app_token)

    def _register_handlers(self) -> None:
        """Register Bolt message and interactive action listeners."""

        @self.app.event("message")
        def on_message(event: dict[str, Any], logger: logging.Logger) -> None:
            self._handle_inbound_message(event)

        @self.app.action(re.compile(r"^promise_.*"))
        def on_promise_action(ack: Callable[[], None], body: dict[str, Any]) -> None:
            ack()
            self._handle_interactive_action(body)

    def _fetch_thread_context(self, channel_id: str, thread_ts: str, current_ts: str) -> list[ContextMessage]:
        """Retrieve recent conversation context within a thread."""
        try:
            resp = self.app.client.conversations_replies(
                channel=channel_id,
                ts=thread_ts,
                limit=10,
            )
            raw_messages = resp.get("messages", [])
            context: list[ContextMessage] = []
            for msg in raw_messages:
                # Exclude current message and bot messages from context
                if msg.get("ts") == current_ts or msg.get("bot_id"):
                    continue
                user = msg.get("user")
                text = msg.get("text", "")
                ts = msg.get("ts", "")
                if user and text:
                    context.append(ContextMessage(user_id=user, text=text, ts=ts))
            return context
        except SlackApiError as err:
            logger.warning("Failed to fetch thread context: %s", err.response.get("error", str(err)))
            return []

    def _handle_inbound_message(self, event: dict[str, Any]) -> None:
        """Normalize raw Slack event and dispatch to the shared pipeline."""
        bot_id = event.get("bot_id")
        user_id = event.get("user")
        subtype = event.get("subtype")

        # Ignore bot-authored messages and self-messages
        if bot_id or user_id == self.bot_user_id:
            return

        # Ignore administrative / unsupported subtypes
        if subtype:
            return

        channel_id = event.get("channel", "")
        text = event.get("text", "")
        event_ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")

        # Retrieve bounded thread context if message is part of a thread
        context_messages: list[ContextMessage] = []
        if thread_ts and thread_ts != event_ts:
            context_messages = self._fetch_thread_context(channel_id, thread_ts, event_ts)

        normalized_event = NormalizedEvent(
            event_id=f"{channel_id}:{event_ts}",
            workspace_id=self.workspace_id,
            channel_id=channel_id,
            author_id=user_id or "",
            text=text,
            event_ts=event_ts,
            thread_ts=thread_ts,
            context_messages=context_messages,
        )

        logger.info(
            "Normalized event from user %s in channel %s (thread: %s)",
            user_id,
            channel_id,
            thread_ts or "none",
        )

        result = self.process_event_fn(normalized_event)
        if not result.should_respond:
            return

        target_thread_ts = thread_ts or event_ts
        if result.promise_card:
            blocks = build_promise_card(result.promise_card)
            fallback_text = f"Promise: {result.promise_card.action}"
            self.app.client.chat_postMessage(
                channel=channel_id,
                thread_ts=target_thread_ts,
                text=fallback_text,
                blocks=blocks,
            )
        elif result.thread_reply_text:
            self.app.client.chat_postMessage(
                channel=channel_id,
                thread_ts=target_thread_ts,
                text=result.thread_reply_text,
            )

    def _handle_interactive_action(self, body: dict[str, Any]) -> None:
        """Handle interactive Block Kit button clicks with deterministic validation."""
        actions = body.get("actions", [])
        if not actions:
            return

        action_data = actions[0]
        action_id = action_data.get("action_id", "")
        promise_id = action_data.get("value", "")
        actor_id = body.get("user", {}).get("id", "")
        channel_id = body.get("channel", {}).get("id", "")
        message = body.get("message", {})
        message_ts = message.get("ts", "")

        # Clean action name (e.g. promise_confirm -> confirm)
        action_name = re.sub(r"^promise_", "", action_id)

        user_action = UserAction(
            action_name=action_name,
            promise_id=promise_id,
            actor_id=actor_id,
            channel_id=channel_id,
            message_ts=message_ts,
        )

        result = self.handle_action_fn(user_action)
        if result.success:
            if result.updated_card:
                blocks = build_promise_card(result.updated_card)
                fallback_text = f"Promise {result.updated_card.status}: {result.updated_card.action}"
                self.app.client.chat_update(
                    channel=channel_id,
                    ts=message_ts,
                    text=fallback_text,
                    blocks=blocks,
                )
        else:
            # Unauthorized or invalid action: inform actor ephemerally
            error_msg = result.error_message or "You are not authorized to perform this action."
            self.app.client.chat_postEphemeral(
                channel=channel_id,
                user=actor_id,
                text=error_msg,
            )

    def send_owner_reminder(self, notification: ReminderNotification) -> bool:
        """Deliver a private overdue reminder card to the promise owner via DM."""
        try:
            open_resp = self.app.client.conversations_open(users=[notification.owner_id])
            dm_channel_id = open_resp.get("channel", {}).get("id")
            if not dm_channel_id:
                logger.error("Failed to open DM channel with user %s", notification.owner_id)
                return False

            blocks = build_reminder_card(notification)
            fallback_text = f"Reminder: You promised to '{notification.action}'"
            self.app.client.chat_postMessage(
                channel=dm_channel_id,
                text=fallback_text,
                blocks=blocks,
            )
            logger.info("Delivered private reminder for promise %s to user %s", notification.promise_id, notification.owner_id)
            return True
        except SlackApiError as err:
            logger.error(
                "Failed to send private reminder to user %s: %s",
                notification.owner_id,
                err.response.get("error", str(err)),
            )
            return False

    def start(self) -> None:
        """Start the Socket Mode listener."""
        logger.info("Starting Slack Socket Mode listener...")
        self.handler.start()

    def stop(self) -> None:
        """Stop the Socket Mode listener."""
        logger.info("Stopping Slack Socket Mode listener...")
        self.handler.close()


def run_slack() -> None:
    """Start the Slack Socket Mode adapter using application settings."""
    from dotenv import load_dotenv

    load_dotenv()
    settings = load_settings(require_model=False)
    if not settings.slack_bot_token or not settings.slack_app_token:
        raise ValueError("Slack bot token and app token must be configured in environment or .env")

    adapter = SlackAdapter(
        bot_token=settings.slack_bot_token,
        app_token=settings.slack_app_token,
    )
    adapter.start()
