"""Validated messages, commitments and processing results."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class ContextMessage(BaseModel):
    """A previous human message in the authorized conversation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    user_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=8000)
    ts: str = Field(pattern=r"^\d{1,10}\.\d{1,6}$")


class NormalizedEvent(BaseModel):
    """Identity comes from transport metadata, not message text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    author_id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=8000)
    event_ts: str = Field(pattern=r"^\d{1,10}\.\d{1,6}$")
    thread_ts: str | None = Field(default=None, pattern=r"^\d{1,10}\.\d{1,6}$")
    received_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    context_messages: tuple[ContextMessage, ...] = Field(default_factory=tuple, max_length=20)

    @property
    def occurred_at(self) -> datetime:
        return datetime.fromtimestamp(float(Decimal(self.event_ts)), timezone.utc)

    @model_validator(mode="after")
    def validate_message(self) -> "NormalizedEvent":
        if not self.text.strip():
            raise ValueError("Message text cannot be blank")
        if any(Decimal(message.ts) >= Decimal(self.event_ts) for message in self.context_messages):
            raise ValueError("Context must precede the current message")
        if tuple(sorted(self.context_messages, key=lambda message: Decimal(message.ts))) != self.context_messages:
            raise ValueError("Context must be chronological")
        if sum(len(message.text) for message in self.context_messages) > 16000:
            raise ValueError("Context exceeds the total text budget")
        return self


class PromiseCardData(BaseModel):
    """Transport-neutral snapshot for a promise card."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    promise_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    action: str = Field(min_length=1, max_length=2000)
    deadline_text: str | None = Field(default=None, max_length=1000)
    deadline_at: AwareDatetime | None = None
    status: Literal["pending_confirmation", "waiting", "confirmed", "completed", "dismissed"] = "pending_confirmation"
    depends_on_promise_id: str | None = Field(default=None, min_length=1)
    depends_on_action: str | None = Field(default=None, max_length=2000)
    relative_deadline_seconds: int | None = Field(default=None, gt=0)


class PromiseRecord(PromiseCardData):
    """Persistent agreement with source, chronology and reminder state."""

    workspace_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    thread_ts: str
    source_message_id: str
    source_occurred_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime
    last_event_at: AwareDatetime
    snoozed_until: AwareDatetime | None = None
    reminder_sent_at: AwareDatetime | None = None
    reminder_attempts: int = Field(default=0, ge=0)
    reminder_retry_at: AwareDatetime | None = None

    def card(self) -> PromiseCardData:
        return PromiseCardData(**{name: getattr(self, name) for name in PromiseCardData.model_fields})


class CardUpdate(BaseModel):
    """A previously delivered card that must reflect an internal state change."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    promise_card: PromiseCardData
    channel_id: str = Field(min_length=1)
    message_ts: str = Field(pattern=r"^\d{1,10}\.\d{1,6}$")


class PipelineResult(BaseModel):
    """Processing success is independent of outbound delivery success."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["processed", "ignored", "duplicate", "failed"] = "processed"
    thread_reply_text: str | None = Field(default=None, min_length=1, max_length=2000)
    promise_card: PromiseCardData | None = None
    error_code: str | None = None

    @property
    def processed(self) -> bool:
        return self.status != "failed"

    @property
    def should_respond(self) -> bool:
        return self.promise_card is not None or self.thread_reply_text is not None

    @model_validator(mode="after")
    def validate_result(self) -> "PipelineResult":
        if self.promise_card is not None and self.thread_reply_text is not None:
            raise ValueError("A result has one outbound response")
        if self.status != "processed" and self.should_respond:
            raise ValueError("Only processed events can produce responses")
        if (self.status == "failed") != (self.error_code is not None):
            raise ValueError("Only failed results require an error code")
        return self


class UserAction(BaseModel):
    """Trusted actor and event metadata accompany an untrusted button value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_name: Literal["confirm", "dismiss", "complete", "snooze", "reschedule"]
    promise_id: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    message_ts: str = Field(pattern=r"^\d{1,10}\.\d{1,6}$")
    occurred_at: AwareDatetime
    received_at: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    deadline_at: AwareDatetime | None = None
    deadline_text: str | None = Field(default=None, max_length=1000)
    remind_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def validate_action(self) -> "UserAction":
        if self.action_name == "reschedule":
            if self.deadline_at is None:
                raise ValueError("Reschedule requires a deadline")
        elif self.deadline_at is not None or self.deadline_text is not None:
            raise ValueError("Only reschedule can change deadline fields")
        if self.remind_at is not None and self.action_name != "snooze":
            raise ValueError("Only snooze can set remind_at")
        return self


class ActionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    error_message: str | None = None
    updated_card: PromiseCardData | None = None
    notification_text: str | None = None
    unblocked_cards: tuple[PromiseCardData, ...] = ()
    unblocked_card_updates: tuple[CardUpdate, ...] = ()

    @model_validator(mode="after")
    def validate_result(self) -> "ActionResult":
        if self.success:
            if self.error_message is not None or self.updated_card is None:
                raise ValueError("Successful actions require a card and no error")
        elif (
            self.error_message is None
            or self.updated_card is not None
            or self.notification_text is not None
            or self.unblocked_cards
            or self.unblocked_card_updates
        ):
            raise ValueError("Failed actions require an error and no successful effects")
        return self


class ReminderNotification(BaseModel):
    """The core chooses the owner; the adapter delivers privately."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    promise_id: str = Field(min_length=1)
    workspace_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    action: str = Field(min_length=1, max_length=2000)
    deadline_text: str | None = None
    channel_id: str
    thread_ts: str


class AgentDecision(BaseModel):
    """One validated model decision; application code executes the effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["ignore", "clarify", "create", "complete", "reschedule"]
    action: str | None = Field(default=None, min_length=1, max_length=2000)
    promise_id: str | None = Field(default=None, min_length=1)
    evidence: str | None = Field(default=None, min_length=1, max_length=2000)
    deadline_text: str | None = Field(default=None, min_length=1, max_length=1000)
    deadline_at: AwareDatetime | None = None
    clarification: str | None = Field(default=None, min_length=1, max_length=2000)
    depends_on_promise_id: str | None = Field(default=None, min_length=1)
    relative_deadline_seconds: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_decision(self) -> "AgentDecision":
        if self.action is not None and not self.action.strip():
            raise ValueError("Action cannot be blank")
        if self.deadline_text is not None and not self.deadline_text.strip():
            raise ValueError("Deadline wording cannot be blank")
        if self.operation in ("create", "complete", "reschedule") and not self.evidence:
            raise ValueError("Writes require source evidence")
        if (self.operation == "create") != (self.action is not None):
            raise ValueError("Only create requires an action")
        if (self.operation in ("complete", "reschedule")) != (self.promise_id is not None):
            raise ValueError("Updates require an existing promise ID")
        if (self.operation == "clarify") != (self.clarification is not None):
            raise ValueError("Only clarify requires clarification text")
        if self.deadline_at is not None and self.deadline_text is None:
            raise ValueError("A parsed deadline requires its original wording")
        if self.operation == "reschedule" and self.deadline_at is None:
            raise ValueError("Reschedule requires an unambiguous deadline")
        if self.operation not in ("create", "reschedule") and (
            self.deadline_at is not None or self.deadline_text is not None
        ):
            raise ValueError("Only create or reschedule can supply a deadline")
        if self.depends_on_promise_id is not None:
            if self.operation != "create":
                raise ValueError("Dependencies can only be established on create")
            if self.depends_on_promise_id == self.promise_id:
                raise ValueError("A promise cannot depend on itself")
        if self.relative_deadline_seconds is not None:
            if self.operation != "create" or self.depends_on_promise_id is None:
                raise ValueError("Relative deadlines require a prerequisite dependency on create")
            if self.deadline_at is not None:
                raise ValueError("Relative deadlines cannot also have an absolute deadline")
        return self
