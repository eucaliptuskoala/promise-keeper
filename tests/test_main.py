from contextlib import closing
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from promise_keeper.__main__ import main
from promise_keeper.config import Settings
from promise_keeper.models import AgentDecision, NormalizedEvent, UserAction
from promise_keeper.pipeline import process_event
from promise_keeper.storage import get_promise, initialize_storage


@pytest.mark.parametrize("enabled_channels", [("C1",), ("*",)])
def test_application_wires_retries_controls_and_reminders(tmp_path, enabled_channels) -> None:
    now = datetime(2026, 9, 12, 13, tzinfo=timezone.utc)
    settings = Settings(openai_api_key="synthetic-key", slack_bot_token="synthetic-bot",
                        slack_app_token="synthetic-app", enabled_channels=enabled_channels,
                        database_path=str(tmp_path / "application.db"))
    event = NormalizedEvent(event_id="Ev1", workspace_id="T1", channel_id="C1", author_id="alice",
                            text="I'll send designs by noon", event_ts=f"{int((now - timedelta(hours=3)).timestamp())}.000001",
                            received_at=now - timedelta(hours=3))
    decision = AgentDecision(operation="create", action="Send designs", evidence="I'll send designs",
                             deadline_text="by noon", deadline_at=now - timedelta(hours=1))
    with patch("promise_keeper.__main__.load_dotenv"), patch("promise_keeper.__main__.load_settings", return_value=settings), \
         patch("promise_keeper.__main__.OpenAI"), patch("promise_keeper.__main__.run_agent", side_effect=lambda event, database, *_: process_event(event, database, lambda *_: decision)), \
         patch("promise_keeper.__main__.datetime") as clock, patch("promise_keeper.adapters.slack.SlackAdapter") as adapter_class:
        clock.now.return_value = now
        adapter = adapter_class.return_value
        adapter.workspace_id = "T1"
        adapter.enabled_channels = ("C1",)
        adapter.deliver_result.return_value = "1789207201.000001"
        adapter.send_owner_reminder.return_value = True

        def run():
            callbacks = adapter_class.call_args.kwargs
            result = callbacks["process_event_fn"](event)
            promise_id = result.promise_card.promise_id
            clock.now.return_value = event.received_at
            callbacks["record_delivery_fn"]("Ev1", "message", None)
            clock.now.return_value = now
            callbacks["tick_fn"]()
            assert {call.args[2] for call in adapter.deliver_result.call_args_list} == {"message", "owner_card"}
            action = UserAction(
                action_name="confirm", promise_id=promise_id, actor_id="alice", workspace_id="T1",
                event_id="click-confirm", channel_id="C1", message_ts="1789207201.000001",
                occurred_at=now - timedelta(minutes=1), received_at=now,
            )
            assert callbacks["handle_action_fn"](action).success
            assert not callbacks["handle_action_fn"](action.model_copy(update={"event_id": "bob-click", "actor_id": "bob"})).success
            callbacks["record_delivery_fn"]("click-confirm", "action", action.message_ts)
            callbacks["tick_fn"]()
            adapter.send_owner_reminder.assert_called_once()
            complete = action.model_copy(update={"action_name": "complete", "event_id": "click-complete", "occurred_at": now})
            assert callbacks["handle_action_fn"](complete).success
            callbacks["record_delivery_fn"]("click-complete", "action", action.message_ts)
            callbacks["tick_fn"]()
            adapter.send_owner_reminder.assert_called_once()
            with closing(initialize_storage(settings.database_path)) as database:
                assert get_promise(database, promise_id).status == "completed"

        adapter.start.side_effect = run
        main([])
