"""Application wiring for model interpretation, SQLite and Slack delivery."""

import argparse
import logging
import signal
from contextlib import closing
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import ValidationError

from promise_keeper.agent import run_agent
from promise_keeper.config import load_settings
from promise_keeper.models import ActionResult, NormalizedEvent, PipelineResult, UserAction
from promise_keeper.pipeline import handle_user_action
from promise_keeper.reminders import check_reminders
from promise_keeper.storage import bind_card, get_promise, initialize_storage, pending_responses, record_delivery

logger = logging.getLogger("promise_keeper")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Track commitments in Slack or run an isolated simulation")
    parser.add_argument("--simulation", action="store_true", help="Run the offline core demo without Slack or keys")
    parser.add_argument(
        "--live-model", action="store_true", help="Use the configured model for synthetic simulation messages",
    )
    arguments = parser.parse_args(argv)
    if arguments.live_model and not arguments.simulation:
        parser.error("--live-model requires --simulation")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if arguments.simulation:
        from promise_keeper.adapters.simulation import run_simulation

        run_simulation(live_model=arguments.live_model)
        return

    load_dotenv()
    try:
        settings = load_settings()
    except (ValidationError, ValueError):
        logger.error("Invalid configuration. Check required keys, MODEL_TIMEOUT_SECONDS and APP_TIMEZONE.")
        raise SystemExit(1) from None
    missing = [
        name for name, value in (
            ("SLACK_BOT_TOKEN", settings.slack_bot_token),
            ("SLACK_APP_TOKEN", settings.slack_app_token),
            ("SLACK_ENABLED_CHANNELS", settings.enabled_channels),
        ) if not value
    ]
    if missing:
        logger.error("Missing configuration: %s.", ", ".join(missing))
        raise SystemExit(1)

    from promise_keeper.adapters.slack import SlackAdapter

    initialize_storage(settings.database_path).close()
    with OpenAI(
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        timeout=settings.model_timeout_seconds,
        max_retries=0,
    ) as client:
        def process(event: NormalizedEvent) -> PipelineResult:
            with closing(initialize_storage(settings.database_path)) as database:
                return run_agent(event, database, client, settings.openai_model, settings.default_timezone)

        def handle_action(action: UserAction) -> ActionResult:
            with closing(initialize_storage(settings.database_path)) as database:
                promise = get_promise(database, action.promise_id)
                if promise is None or promise.channel_id not in adapter.enabled_channels:
                    return ActionResult(success=False, error_message="Promise not found in an enabled channel.")
                return handle_user_action(action, database)

        def acknowledge_delivery(event_id: str, kind: str, message_ts: str | None) -> None:
            with closing(initialize_storage(settings.database_path)) as database:
                record_delivery(database, adapter.workspace_id, event_id, kind, message_ts, datetime.now(timezone.utc))

        def acknowledge_reminder(promise_id: str, channel_id: str, message_ts: str) -> None:
            with closing(initialize_storage(settings.database_path)) as database:
                with database:
                    bind_card(database, adapter.workspace_id, channel_id, message_ts, promise_id)

        def tick() -> None:
            now = datetime.now(timezone.utc)
            with closing(initialize_storage(settings.database_path)) as database:
                for row in pending_responses(database, adapter.workspace_id, now):
                    if row["kind"] == "message":
                        result = PipelineResult.model_validate_json(row["result_json"])
                        card = result.promise_card
                    else:
                        result = ActionResult.model_validate_json(row["result_json"])
                        card = result.updated_card
                    if card:
                        promise = get_promise(database, card.promise_id)
                        if promise is None or promise.channel_id not in adapter.enabled_channels:
                            continue
                        updates = {"promise_card" if row["kind"] == "message" else "updated_card": promise.card()}
                        result = type(result).model_validate({**result.model_dump(), **updates})
                    elif row["channel_id"] not in adapter.enabled_channels:
                        continue
                    message_ts = adapter.deliver_result(row["channel_id"], row["target_ts"], row["kind"], result)
                    record_delivery(
                        database, adapter.workspace_id, row["event_id"], row["kind"], message_ts, now,
                    )
                check_reminders(database, adapter.send_owner_reminder, now, adapter.workspace_id, adapter.enabled_channels)

        adapter = SlackAdapter(
            bot_token=settings.slack_bot_token, app_token=settings.slack_app_token,
            process_event_fn=process, handle_action_fn=handle_action, enabled_channels=settings.enabled_channels,
            record_delivery_fn=acknowledge_delivery, record_reminder_fn=acknowledge_reminder, tick_fn=tick,
        )

        def shutdown(signum, frame) -> None:
            adapter.stop()

        previous_handlers = {name: signal.signal(name, shutdown) for name in (signal.SIGINT, signal.SIGTERM)}
        try:
            adapter.start()
        finally:
            for name, handler in previous_handlers.items():
                signal.signal(name, handler)


if __name__ == "__main__":
    main()
