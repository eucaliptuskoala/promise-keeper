"""Shared boundary and domain models for Promise Keeper."""

from datetime import datetime, timezone
from pydantic import BaseModel, ConfigDict, Field


class ContextMessage(BaseModel):
    """A previous message in the conversation context."""

    model_config = ConfigDict(frozen=True)

    user_id: str
    text: str
    ts: str


class NormalizedEvent(BaseModel):
    """Transport-independent inbound message event."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    workspace_id: str
    channel_id: str
    author_id: str
    text: str
    event_ts: str
    thread_ts: str | None = None
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    context_messages: list[ContextMessage] = Field(default_factory=list)


class PromiseCardData(BaseModel):
    """Data required to render or update a promise Block Kit card."""

    model_config = ConfigDict(frozen=True)

    promise_id: str
    owner_id: str
    action: str
    deadline_text: str | None = None
    status: str = "pending_confirmation"  # pending_confirmation, confirmed, completed, dismissed


class PipelineResult(BaseModel):
    """Result of processing one normalized event."""

    model_config = ConfigDict(frozen=True)

    processed: bool = True
    should_respond: bool = False
    thread_reply_text: str | None = None
    promise_card: PromiseCardData | None = None


class UserAction(BaseModel):
    """Interactive action from a user (e.g., Slack button click)."""

    model_config = ConfigDict(frozen=True)

    action_name: str  # "confirm", "dismiss", "complete", "snooze"
    promise_id: str
    actor_id: str
    channel_id: str
    message_ts: str


class ActionResult(BaseModel):
    """Result of handling an interactive user action."""

    model_config = ConfigDict(frozen=True)

    success: bool
    error_message: str | None = None
    updated_card: PromiseCardData | None = None
    notification_text: str | None = None


class ReminderNotification(BaseModel):
    """Data required to send a private reminder for an overdue promise."""

    model_config = ConfigDict(frozen=True)

    promise_id: str
    owner_id: str
    action: str
    deadline_text: str | None = None
    channel_id: str | None = None
    thread_ts: str | None = None
