"""Slack Bolt transport adapter with Socket Mode."""

import json
import logging
import re
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal
from html import escape
from threading import Event, RLock, Thread
from typing import Any, Callable

from slack_bolt import App

try:
    from slack_bolt.adapter.socket_mode.websocket_client import SocketModeHandler
except ImportError:
    from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

from promise_keeper.models import (
    ActionResult,
    ContextMessage,
    LeaderboardEntry,
    NormalizedEvent,
    PipelineResult,
    PromiseCardData,
    ReminderNotification,
    UserAction,
)

logger = logging.getLogger("promise_keeper.adapters.slack")


def build_promise_card(
    card: PromiseCardData, *, show_controls: bool = False, source_url: str | None = None,
) -> list[dict[str, Any]]:
    """Build Slack Block Kit representation of a promise card."""
    status_display = {
        "pending_confirmation": "Pending confirmation :hourglass_flowing_sand:",
        "waiting": "Waiting on prerequisite :hourglass:",
        "confirmed": "Confirmed :white_check_mark:",
        "completed": "Completed :tada:",
        "dismissed": "Dismissed :heavy_multiplication_x:",
    }.get(card.status, card.status)

    deadline_display = escape(card.deadline_text or "Not specified", quote=False)[:300]
    action_display = escape(card.action, quote=False)[:1800]
    if card.deadline_at is not None:
        fallback = card.deadline_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        deadline_display += f" (<!date^{int(card.deadline_at.timestamp())}^{{date_num}} {{time}}|{fallback}>)"
    elif card.deadline_text:
        deadline_display += " (needs clarification)"

    prerequisite_line = ""
    if card.depends_on_action:
        prerequisite_display = escape(card.depends_on_action, quote=False)[:300]
        prerequisite_line = f"*Prerequisite:* {prerequisite_display}\n"

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Promise Detected* :handshake:\n"
                    f"*Owner:* <@{card.owner_id}>\n"
                    f"*Action:* {action_display}\n"
                    f"{prerequisite_line}"
                    f"*Deadline:* {deadline_display}\n"
                    f"*Status:* {status_display}"
                ),
            },
        }
    ]

    if show_controls and card.channel_id:
        source_display = f"<#{card.channel_id}>"
        if source_url:
            source_display += f" · <{escape(source_url, quote=False)}|View original message>"
        blocks[0]["text"]["text"] += f"\n*Promised in:* {source_display}"
    if not show_controls:
        return blocks

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
    elif card.status == "waiting":
        elements.extend(
            [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Change deadline"},
                    "action_id": "promise_reschedule",
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
                    "text": {"type": "plain_text", "text": "Change deadline"},
                    "action_id": "promise_reschedule",
                    "value": card.promise_id,
                },
            ]
        )

    if elements:
        blocks.append({"type": "actions", "elements": elements})


    return blocks


def build_reminder_card(notification: ReminderNotification, source_url: str | None = None) -> list[dict[str, Any]]:
    """Build Slack Block Kit representation for an overdue private reminder."""
    deadline_display = escape(notification.deadline_text or "Overdue", quote=False)[:300]
    action_display = escape(notification.action, quote=False)[:1800]
    if notification.deadline_at is not None:
        fallback = notification.deadline_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        deadline_display += f" (<!date^{int(notification.deadline_at.timestamp())}^{{date_num}} {{time}}|{fallback}>)"
    source_display = f"<#{notification.channel_id}>"
    if source_url:
        source_display += f" · <{escape(source_url, quote=False)}|View original message>"

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*Reminder: Overdue Commitment* :alarm_clock:\n\n"
                    f"You promised: *{action_display}*\n"
                    f"*Promised in:* {source_display}\n"
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
                    "text": {"type": "plain_text", "text": "Snooze 1 hour"},
                    "action_id": "promise_snooze",
                    "value": notification.promise_id,
                },
            ],
        },
    ]
    return blocks


def _detect_dominant_language(stats: list[LeaderboardEntry]) -> str:
    actions = [action for entry in stats for action in entry.get("sample_actions", [])]
    if not actions:
        return "en"
    ru_count = sum(1 for action in actions if re.search(r"[\u0400-\u04FF]", action))
    return "ru" if ru_count > len(actions) / 2 else "en"


def build_leaderboard_card(
    stats: list[LeaderboardEntry], is_monthly: bool = False, language: str | None = None,
) -> list[dict[str, Any]]:
    """Build Slack Block Kit representation for unfulfilled commitments leaderboard."""
    lang = language or _detect_dominant_language(stats)
    if lang == "ru":
        title = "*Ежемесячный отчёт по сорванным дедлайнам* :trophy:" if is_monthly else "*Доска фуфлыжников (Анти-топ сорванных дедлайнов)* :trophy:"
        subtitle = "Итоги месяца по невыполненным обязательствам в канале." if is_monthly else "Текущий список участников с просроченными обещаниями."
        empty_text = ":tada: *Все молодцы!* В этом канале нет сорванных дедлайнов. Все обещания закрыты вовремя."
    else:
        title = "*Monthly Missed Deadlines Report* :trophy:" if is_monthly else "*Wall of Shame (Overdue Commitments)* :trophy:"
        subtitle = "Monthly summary of unfulfilled commitments in this channel." if is_monthly else "Current list of members with overdue promises."
        empty_text = ":tada: *Great job, everyone!* There are no missed deadlines in this channel. All commitments were completed on time."

    blocks: list[dict[str, Any]] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"{title}\n_{subtitle}_",
            },
        },
        {"type": "divider"},
    ]

    if not stats:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": empty_text,
                },
            }
        )
        return blocks

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for rank, entry in enumerate(stats, start=1):
        medal = medals[rank - 1] if rank <= 3 else "🔹"
        count = entry["overdue_count"]
        if lang == "ru":
            if count % 10 == 1 and count % 100 != 11:
                word = "просроченное обещание"
            elif 2 <= count % 10 <= 4 and (count % 100 < 10 or count % 100 >= 20):
                word = "просроченных обещания"
            else:
                word = "просроченных обещаний"
            sample = ""
            if entry.get("sample_actions"):
                action_escaped = escape(entry["sample_actions"][0], quote=False)[:100]
                sample = f"\n    _«{action_escaped}»_"
        else:
            word = "overdue commitment" if count == 1 else "overdue commitments"
            sample = ""
            if entry.get("sample_actions"):
                action_escaped = escape(entry["sample_actions"][0], quote=False)[:100]
                sample = f"\n    _\"{action_escaped}\"_"

        lines.append(f"{medal} *{rank}.* <@{entry['owner_id']}> — *{count}* {word}{sample}")

    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "\n\n".join(lines),
            },
        }
    )
    return blocks


class SlackAdapter:
    """Normalize input and deliver core results through Slack."""

    def __init__(
        self,
        bot_token: str,
        app_token: str,
        process_event_fn: Callable[[NormalizedEvent], PipelineResult],
        handle_action_fn: Callable[[UserAction], ActionResult],
        enabled_channels: tuple[str, ...],
        record_delivery_fn: Callable[[str, str, str | None], None],
        record_reminder_fn: Callable[[str, str, str], None],
        tick_fn: Callable[[], None] | None = None,
        stats_fn: Callable[[str, str], list[LeaderboardEntry]] | None = None,
        claim_stats_fn: Callable[[str, str, str], bool] | None = None,
        channel_language_fn: Callable[[str, str], str] | None = None,
        format_leaderboard_fn: Callable[[list[LeaderboardEntry], bool], list[dict[str, Any]]] | None = None,
    ) -> None:
        self.process_event_fn = process_event_fn
        self.handle_action_fn = handle_action_fn
        self.enabled_channels = enabled_channels
        self.record_delivery_fn = record_delivery_fn
        self.record_reminder_fn = record_reminder_fn
        self.tick_fn = tick_fn
        self.stats_fn = stats_fn
        self.claim_stats_fn = claim_stats_fn
        self.channel_language_fn = channel_language_fn
        self.format_leaderboard_fn = format_leaderboard_fn
        self._processing_lock = RLock()

        self._stopped = Event()
        self._ticker: Thread | None = None
        self._context: OrderedDict[tuple[str, str], tuple[ContextMessage, ...]] = OrderedDict()
        self.app = App(token=bot_token)
        self.app.client.timeout = 10
        self.app.client.retry_handlers = []
        auth = self.app.client.auth_test()
        self.bot_user_id = auth.get("user_id", "")
        self.workspace_id = auth.get("team_id", "")
        if not self.bot_user_id or not self.workspace_id:
            raise ValueError("Slack authentication did not return bot and workspace identities")
        if enabled_channels == ("*",):
            channels = []
            cursor = None
            for _ in range(20):
                response = self.app.client.conversations_list(
                    types="public_channel", exclude_archived=True, limit=200, cursor=cursor,
                )
                channels.extend(
                    channel["id"] for channel in response.get("channels", [])
                    if channel.get("id") and channel.get("is_channel")
                    and not channel.get("is_private") and not channel.get("is_archived")
                )
                cursor = response.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
            else:
                raise ValueError("Public channel listing exceeded the startup page limit")
            self.enabled_channels = tuple(dict.fromkeys(channels))
            if not self.enabled_channels:
                raise ValueError("No active public channels are available to the bot")
            logger.info("Enabled all %d public channels; the bot receives messages where it is invited", len(self.enabled_channels))
        self._register_handlers()
        self.handler = SocketModeHandler(self.app, app_token)

    def _register_handlers(self) -> None:
        @self.app.event("message")
        def on_message(event: dict[str, Any], body: dict[str, Any]) -> None:
            try:
                with self._processing_lock:
                    self._handle_inbound_message(event, body.get("event_id"))
            except Exception as error:
                logger.error("Message processing failed (%s)", type(error).__name__)

        @self.app.action(re.compile(r"^promise_(confirm|dismiss|complete|snooze|reschedule)$"))
        def on_promise_action(ack: Callable[[], None], body: dict[str, Any]) -> None:
            ack()
            try:
                self._handle_interactive_action(body)
            except Exception as error:
                logger.error("Action processing failed (%s)", type(error).__name__)

        @self.app.view("promise_reschedule_submit")
        def on_reschedule(ack: Callable[..., None], body: dict[str, Any]) -> None:
            view = body.get("view", {})
            values = view.get("state", {}).get("values", {})
            selected = (
                values.get("deadline", {})
                .get("deadline_at", {})
                .get("selected_date_time")
            )
            if selected is None:
                ack(response_action="errors", errors={"deadline": "Please select a date and time."})
                return
            now_ts = datetime.now(timezone.utc).timestamp()
            if selected <= now_ts:
                ack(response_action="errors", errors={"deadline": "Deadline must be in the future."})
                return

            ack()
            try:
                self._handle_reschedule_submission(body)
            except Exception as error:
                logger.error("Deadline processing failed (%s)", type(error).__name__)

    def _fetch_thread_context(self, channel_id: str, thread_ts: str, current_ts: str) -> list[ContextMessage]:
        try:
            response = self.app.client.conversations_replies(
                channel=channel_id, ts=thread_ts, latest=current_ts, inclusive=False, limit=20,
            )
        except SlackApiError as error:
            code = error.response.get("error")
            if code not in ("missing_scope", "not_allowed_token_type"):
                raise
            logger.warning("Full thread history unavailable with this token; fetching the parent message")
            response = self.app.client.conversations_history(
                channel=channel_id, oldest=thread_ts, latest=thread_ts, inclusive=True, limit=1,
            )
        return self._normalize_context(response.get("messages", []), current_ts)

    def _normalize_context(self, messages: list[dict[str, Any]], current_ts: str) -> list[ContextMessage]:
        context = []
        for message in messages:
            if message.get("bot_id") or message.get("subtype") or message.get("user") == self.bot_user_id:
                continue
            if not message.get("user") or not message.get("text", "").strip() or not message.get("ts"):
                continue
            if Decimal(message["ts"]) >= Decimal(current_ts):
                continue
            context.append(ContextMessage(user_id=message["user"], text=message["text"][:8000], ts=message["ts"]))
        return sorted(context, key=lambda message: Decimal(message.ts))

    def is_stats_command(self, text: str) -> bool:
        lower = text.lower()
        if self.bot_user_id and f"<@{self.bot_user_id.lower()}>" in lower and "stats" in lower:
            return True
        if "@promise keeper" in lower and "stats" in lower:
            return True
        if re.search(r"^\s*!stats\b", lower):
            return True
        return False

    def send_channel_leaderboard(
        self, channel_id: str, stats: list[LeaderboardEntry], is_monthly: bool = True, language: str | None = None,
    ) -> str | None:
        if channel_id not in self.enabled_channels:
            return None
        lang = language or (self.channel_language_fn(self.workspace_id, channel_id) if self.channel_language_fn else None)
        if self.format_leaderboard_fn:
            blocks = self.format_leaderboard_fn(stats, is_monthly)
        else:
            blocks = build_leaderboard_card(stats, is_monthly=is_monthly, language=lang)
        fallback_text = (
            ("Ежемесячный отчёт по сорванным дедлайнам" if is_monthly else "Доска фуфлыжников (Анти-топ сорванных дедлайнов)")
            if lang == "ru"
            else ("Monthly Missed Deadlines Report" if is_monthly else "Wall of Shame (Overdue Commitments)")
        )
        try:
            response = self.app.client.chat_postMessage(
                channel=channel_id,
                text=fallback_text,
                blocks=blocks,
            )
            return response.get("ts")
        except SlackApiError as error:
            logger.warning("Failed to post channel leaderboard (%s)", error.response.get("error", "slack_error"))
            return None

    def _handle_inbound_message(self, event: dict[str, Any], event_id: str | None = None) -> None:
        user_id = event.get("user")
        channel_id = event.get("channel")
        if event.get("bot_id") or event.get("subtype") or user_id == self.bot_user_id:
            return
        text = event.get("text", "").strip()
        if channel_id not in self.enabled_channels or not user_id or not text:
            return
        event_ts = event.get("ts", "")
        thread_ts = event.get("thread_ts")
        if self.is_stats_command(text):
            stats_event_id = event_id or f"{channel_id}:{event_ts}:stats"
            if self.claim_stats_fn and not self.claim_stats_fn(self.workspace_id, stats_event_id, channel_id):
                return
            stats = self.stats_fn(self.workspace_id, channel_id) if self.stats_fn else []
            lang = self.channel_language_fn(self.workspace_id, channel_id) if self.channel_language_fn else None
            if self.format_leaderboard_fn:
                blocks = self.format_leaderboard_fn(stats, False)
            else:
                blocks = build_leaderboard_card(stats, is_monthly=False, language=lang)
            fallback_text = (
                "Доска фуфлыжников (Анти-топ сорванных дедлайнов)"
                if lang == "ru"
                else "Wall of Shame (Overdue Commitments)"
            )
            try:
                self.app.client.chat_postMessage(
                    channel=channel_id,
                    thread_ts=thread_ts,
                    text=fallback_text,
                    blocks=blocks,
                )
            except SlackApiError as error:
                logger.warning("Failed to post stats leaderboard (%s)", error.response.get("error", "slack_error"))
            return
        key = (channel_id, thread_ts or "channel")
        context = list(self._context.get(key, ()))

        try:
            if thread_ts and thread_ts != event_ts:
                context += self._fetch_thread_context(channel_id, thread_ts, event_ts)
            elif not context:
                response = self.app.client.conversations_history(
                    channel=channel_id, latest=event_ts, inclusive=False, limit=20,
                )
                context += self._normalize_context(response.get("messages", []), event_ts)
        except SlackApiError as error:
            logger.warning("Context retrieval failed (%s)", error.response.get("error", "slack_error"))
        unique = {message.ts: message for message in context if Decimal(message.ts) < Decimal(event_ts)}
        bounded = []
        remaining = 16000
        for message in sorted(unique.values(), key=lambda message: Decimal(message.ts), reverse=True)[:20]:
            if len(message.text) > remaining:
                break
            bounded.append(message)
            remaining -= len(message.text)
        normalized = NormalizedEvent(
            event_id=event_id or f"{channel_id}:{event_ts}", workspace_id=self.workspace_id,
            channel_id=channel_id, author_id=user_id, text=event["text"], event_ts=event_ts,
            thread_ts=thread_ts, context_messages=tuple(reversed(bounded)),
        )
        result = self.process_event_fn(normalized)
        unique[event_ts] = ContextMessage(user_id=user_id, text=event["text"], ts=event_ts)
        self._context[key] = tuple(sorted(unique.values(), key=lambda message: Decimal(message.ts))[-20:])
        self._context.move_to_end(key)
        if len(self._context) > 100:
            self._context.popitem(last=False)
        if result.should_respond:
            delivered_ts = self.deliver_result(channel_id, thread_ts or event_ts, "message", result)
            self.record_delivery_fn(normalized.event_id, "message", delivered_ts)
            if self.tick_fn:
                self.tick_fn()

    def deliver_result(
        self, channel_id: str, target_ts: str, kind: str, result: PipelineResult | ActionResult,
    ) -> str | None:
        card = result.promise_card if isinstance(result, PipelineResult) else result.updated_card
        text = f"Promise {card.status}: {card.action}" if card else result.thread_reply_text
        try:
            source_url = None
            if kind == "owner_card":
                if card is None:
                    return None
                response = self.app.client.conversations_open(users=[card.owner_id])
                channel_id = response.get("channel", {}).get("id")
                if not channel_id or not channel_id.startswith("D"):
                    return None
            if card and channel_id.startswith("D") and card.channel_id and card.source_message_id:
                try:
                    source = self.app.client.chat_getPermalink(
                        channel=card.channel_id, message_ts=card.source_message_id,
                    )
                    source_url = source.get("permalink")
                except SlackApiError as error:
                    logger.warning("Original message link unavailable (%s)", error.response.get("error", "slack_error"))
                except Exception as error:
                    logger.warning("Original message link unavailable (%s)", type(error).__name__)
            arguments: dict[str, Any] = {
                "channel": channel_id, "text": escape(text, quote=False),
            }
            if card:
                arguments["blocks"] = build_promise_card(
                    card, show_controls=channel_id.startswith("D"), source_url=source_url,
                )
            if kind == "action" or kind.startswith("card_update:"):
                response = self.app.client.chat_update(ts=target_ts, **arguments)
            elif kind == "owner_card":
                response = self.app.client.chat_postMessage(unfurl_links=False, unfurl_media=False, **arguments)
                if response.get("ts"):
                    self.record_reminder_fn(card.promise_id, channel_id, response["ts"])
            else:
                response = self.app.client.chat_postMessage(
                    thread_ts=target_ts, unfurl_links=False, unfurl_media=False, **arguments,
                )
            return response.get("ts")
        except SlackApiError as error:
            logger.warning("Outbound delivery failed (%s)", error.response.get("error", "slack_error"))
            return None
        except Exception as error:
            logger.warning("Outbound delivery failed (%s)", type(error).__name__)
            return None

    def _handle_interactive_action(self, body: dict[str, Any]) -> None:
        if body.get("team", {}).get("id") != self.workspace_id:
            return
        actions = body.get("actions", [])
        if not actions:
            return
        payload = actions[0]
        if payload.get("action_id") == "promise_reschedule":
            if not payload.get("action_ts"):
                raise ValueError("Interactive action lacks its original timestamp")
            self.app.client.views_open(trigger_id=body["trigger_id"], view={
                "type": "modal", "callback_id": "promise_reschedule_submit",
                "title": {"type": "plain_text", "text": "Change deadline"},
                "submit": {"type": "plain_text", "text": "Save"},
                "close": {"type": "plain_text", "text": "Cancel"},
                "private_metadata": json.dumps({
                    "promise_id": payload["value"],
                    "channel_id": body["channel"]["id"],
                    "message_ts": body["message"]["ts"],
                    "occurred_at": datetime.fromtimestamp(
                        float(Decimal(payload["action_ts"])), timezone.utc,
                    ).isoformat(),
                }),
                "blocks": [{
                    "type": "input",
                    "block_id": "deadline",
                    "label": {"type": "plain_text", "text": "New deadline"},
                    "element": {"type": "datetimepicker", "action_id": "deadline_at"},
                }],
            })
            return
        if not payload.get("action_ts"):
            raise ValueError("Interactive action lacks its original timestamp")
        action = UserAction(
            action_name=re.sub(r"^promise_", "", payload.get("action_id", "")), promise_id=payload.get("value", ""),
            actor_id=body.get("user", {}).get("id", ""), workspace_id=self.workspace_id,
            event_id=f"{body['channel']['id']}:{body['message']['ts']}:{payload['action_id']}:{payload['action_ts']}",
            channel_id=body["channel"]["id"], message_ts=body["message"]["ts"],
            occurred_at=datetime.fromtimestamp(float(Decimal(payload["action_ts"])), timezone.utc),
        )
        self._dispatch_action(action)

    def _handle_reschedule_submission(self, body: dict[str, Any]) -> None:
        if body.get("team", {}).get("id") != self.workspace_id:
            return
        view = body["view"]
        metadata = json.loads(view["private_metadata"])
        selected = view["state"]["values"]["deadline"]["deadline_at"]["selected_date_time"]
        if selected is None:
            return
        now = datetime.now(timezone.utc)
        action = UserAction(
            action_name="reschedule", promise_id=metadata["promise_id"], actor_id=body["user"]["id"],
            workspace_id=self.workspace_id, event_id=f"view:{view['id']}:{view['hash']}",
            channel_id=metadata["channel_id"],
            message_ts=metadata["message_ts"],
            occurred_at=metadata["occurred_at"],
            received_at=now,
            deadline_at=datetime.fromtimestamp(selected, timezone.utc),
        )
        self._dispatch_action(action)

    def _dispatch_action(self, action: UserAction) -> None:
        with self._processing_lock:
            result = self.handle_action_fn(action)
            if result.success:
                delivered_ts = self.deliver_result(action.channel_id, action.message_ts, "action", result)
                self.record_delivery_fn(action.event_id, "action", delivered_ts)
                if self.tick_fn:
                    self.tick_fn()
        if result.success:
            if result.notification_text:
                self.app.client.chat_postEphemeral(
                    channel=action.channel_id, user=action.actor_id, text=result.notification_text,
                )
        else:
            self.app.client.chat_postEphemeral(
                channel=action.channel_id, user=action.actor_id, text=result.error_message,
            )

    def send_owner_reminder(self, notification: ReminderNotification) -> bool:
        if notification.workspace_id != self.workspace_id or notification.channel_id not in self.enabled_channels:
            return False
        try:
            response = self.app.client.conversations_open(users=[notification.owner_id])
            channel_id = response.get("channel", {}).get("id")
            if not channel_id:
                return False
            source_url = None
            if notification.source_message_id is not None:
                try:
                    source = self.app.client.chat_getPermalink(
                        channel=notification.channel_id, message_ts=notification.source_message_id,
                    )
                    source_url = source.get("permalink")
                except SlackApiError as error:
                    logger.warning("Original message link unavailable (%s)", error.response.get("error", "slack_error"))
                except Exception as error:
                    logger.warning("Original message link unavailable (%s)", type(error).__name__)
            message = self.app.client.chat_postMessage(
                channel=channel_id,
                text=escape(f"Reminder: {notification.action}", quote=False),
                blocks=build_reminder_card(notification, source_url),
                unfurl_links=False,
                unfurl_media=False,
            )
            if not message.get("ts"):
                return False
            with self._processing_lock:
                self.record_reminder_fn(notification.promise_id, channel_id, message["ts"])
            return True
        except SlackApiError as error:
            logger.warning("Private reminder delivery failed (%s)", error.response.get("error", "slack_error"))
            return False
        except Exception as error:
            logger.warning("Private reminder delivery failed (%s)", type(error).__name__)
            return False

    def _run_ticks(self) -> None:
        while not self._stopped.wait(30):
            try:
                with self._processing_lock:
                    self.tick_fn()
            except Exception as error:
                logger.error("Periodic check failed (%s)", type(error).__name__)

    def start(self) -> None:
        if self.tick_fn is not None:
            self._ticker = Thread(target=self._run_ticks, name="promise-reminders", daemon=True)
            self._ticker.start()
        try:
            self.handler.connect()
            while not self._stopped.wait(0.5):
                pass
        finally:
            self.stop()

    def stop(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self.handler.close()
        if self._ticker is not None:
            self._ticker.join(timeout=5)


def run_slack() -> None:
    from promise_keeper.__main__ import main

    main([])
