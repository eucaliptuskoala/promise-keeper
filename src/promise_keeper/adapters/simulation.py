"""Alice, Bob and Andrii enter the real core with isolated synthetic data."""

import tempfile
from contextlib import closing, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

from promise_keeper.agent import interpret_message
from promise_keeper.config import load_settings
from promise_keeper.models import AgentDecision, ContextMessage, NormalizedEvent, UserAction
from promise_keeper.pipeline import handle_user_action, process_event
from promise_keeper.reminders import check_reminders
from promise_keeper.storage import get_promise, initialize_storage, record_delivery


def run_simulation(live_model: bool = False) -> None:
    """Scripted interpretation is explicit; only synthetic effects are captured."""
    settings = None
    if live_model:
        load_dotenv()
        settings = load_settings()
    now = datetime(2026, 9, 12, 10, tzinfo=timezone.utc)
    bob_ts = f"{int(now.timestamp()) - 60}.000001"
    alice_ts = f"{int(now.timestamp())}.000001"
    card_ts = f"{int(now.timestamp()) + 1}.000001"
    events = (
        NormalizedEvent(
            event_id="simulation-request", workspace_id="simulation", channel_id="demo", author_id="bob",
            text="Can you send the designs by noon today?", event_ts=bob_ts, received_at=now - timedelta(minutes=1),
        ),
        NormalizedEvent(
            event_id="simulation-create", workspace_id="simulation", channel_id="demo", author_id="alice",
            text="I'll send the designs by noon today.", event_ts=alice_ts, thread_ts=bob_ts, received_at=now,
            context_messages=(ContextMessage(user_id="bob", text="Can you send the designs by noon today?", ts=bob_ts),),
        ),
        NormalizedEvent(
            event_id="simulation-intention", workspace_id="simulation", channel_id="demo", author_id="andrii",
            text="I might look into animations.", event_ts=f"{int(now.timestamp()) + 60}.000001",
            received_at=now + timedelta(minutes=1),
        ),
    )
    scripted = {
        "simulation-request": AgentDecision(operation="ignore"),
        "simulation-create": AgentDecision(
            operation="create", action="Send the designs", evidence="I'll send the designs",
            deadline_text="by noon today", deadline_at=now.replace(hour=12),
        ),
        "simulation-intention": AgentDecision(operation="ignore"),
    }
    client_context = OpenAI(
        api_key=settings.openai_api_key, base_url=settings.openai_base_url,
        timeout=settings.model_timeout_seconds, max_retries=0,
    ) if settings else nullcontext()
    with client_context as client, tempfile.TemporaryDirectory(prefix="promise-keeper-simulation-") as directory:
        print("Mode: live model, synthetic data, no Slack delivery" if live_model
              else "Mode: offline, scripted model decisions, no Slack delivery")
        path = str(Path(directory) / "synthetic.db")
        with closing(initialize_storage(path)) as database:
            promise_id = None
            for event in events:
                result = process_event(event, database, lambda source, promises: interpret_message(
                    source, promises, client, settings.openai_model, settings.default_timezone,
                ) if live_model else scripted[source.event_id])
                print(f"{event.author_id}: {result.status}")
                if result.status == "failed":
                    raise SystemExit(f"Simulation failed: {result.error_code}")
                if event.author_id != "alice" and result.should_respond:
                    raise SystemExit("The request and vague intention must not produce responses")
                if event.author_id == "alice" and result.promise_card:
                    promise_id = result.promise_card.promise_id
                    record_delivery(database, "simulation", event.event_id, "message", card_ts, now)
            if promise_id is None:
                raise SystemExit("Simulation did not create Alice's promise")
            confirm = UserAction(
                action_name="confirm", promise_id=promise_id, actor_id="alice", workspace_id="simulation",
                event_id="simulation-confirm", channel_id="demo", message_ts=card_ts,
                occurred_at=now + timedelta(minutes=2), received_at=now + timedelta(minutes=2),
            )
            confirmed = handle_user_action(confirm, database)
            if not confirmed.success:
                raise SystemExit(confirmed.error_message)
            print(f"Create -> confirm: {confirmed.updated_card.status}")
        with closing(initialize_storage(path)) as database:
            print(f"After reopening SQLite: {get_promise(database, promise_id).status}")
            reminders = []
            check_reminders(
                database, lambda notification: reminders.append(notification) or True,
                now + timedelta(hours=3), "simulation", ("demo",),
            )
            print(f"Captured private reminders: {len(reminders)}")
            if len(reminders) != 1:
                raise SystemExit("Expected one overdue reminder for Alice")
            complete = confirm.model_copy(update={
                "action_name": "complete", "event_id": "simulation-complete",
                "occurred_at": now + timedelta(hours=4), "received_at": now + timedelta(hours=4),
            })
            completed = handle_user_action(complete, database)
            if not completed.success:
                raise SystemExit(completed.error_message)
            print(f"Complete: {completed.updated_card.status}")
            count = check_reminders(
                database, lambda notification: True, now + timedelta(days=1), "simulation", ("demo",),
            )
            print(f"Reminders after completion: {count}")
            if count:
                raise SystemExit("Completed promises must not produce reminders")
