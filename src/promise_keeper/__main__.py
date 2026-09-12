"""Application wiring for model interpretation, SQLite and Slack delivery."""

import argparse
import logging
import signal
from contextlib import closing
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from openai import OpenAI
from pydantic import ValidationError

from promise_keeper.agent import run_agent
from promise_keeper.config import load_settings
from promise_keeper.models import ActionResult, LeaderboardEntry, NormalizedEvent, PipelineResult, UserAction
from promise_keeper.pipeline import handle_user_action
from promise_keeper.reminders import check_reminders
from promise_keeper.storage import (
    bind_card,
    claim_stats_command,
    get_last_monthly_report,
    get_monthly_missed_deadline_stats,
    get_monthly_report_attempts,
    get_promise,
    get_unfulfilled_stats,
    initialize_storage,
    open_storage,
    pending_responses,
    record_delivery,
    record_monthly_report,
    record_monthly_report_attempt,
)


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
            with closing(open_storage(settings.database_path)) as database:
                return run_agent(event, database, client, settings.openai_model, settings.default_timezone)

        def handle_action(action: UserAction) -> ActionResult:
            with closing(open_storage(settings.database_path)) as database:
                promise = get_promise(database, action.promise_id)
                if promise is None or promise.channel_id not in adapter.enabled_channels:
                    return ActionResult(success=False, error_message="Promise not found in an enabled channel.")
                return handle_user_action(action, database)

        def acknowledge_delivery(event_id: str, kind: str, message_ts: str | None) -> None:
            with closing(open_storage(settings.database_path)) as database:
                record_delivery(database, adapter.workspace_id, event_id, kind, message_ts, datetime.now(timezone.utc))

        def acknowledge_reminder(promise_id: str, channel_id: str, message_ts: str) -> None:
            with closing(open_storage(settings.database_path)) as database:
                with database:
                    bind_card(database, adapter.workspace_id, channel_id, message_ts, promise_id)

        def get_stats(workspace_id: str, channel_id: str) -> list[LeaderboardEntry]:
            now = datetime.now(timezone.utc)
            with closing(open_storage(settings.database_path)) as database:
                return get_unfulfilled_stats(database, workspace_id, channel_id, now)

        def claim_stats(workspace_id: str, event_id: str, channel_id: str) -> bool:
            with closing(open_storage(settings.database_path)) as database:
                with database:
                    return claim_stats_command(database, workspace_id, event_id, channel_id, datetime.now(timezone.utc))


        def tick() -> None:
            now = datetime.now(timezone.utc)
            tz = timezone.utc if settings.default_timezone == "UTC" else ZoneInfo(settings.default_timezone)
            current_month_key = now.astimezone(tz).strftime("%Y-%m")
            with closing(open_storage(settings.database_path)) as database:
                for row in pending_responses(database, adapter.workspace_id, now):
                    if row["kind"] in ("message", "owner_card"):
                        result = PipelineResult.model_validate_json(row["result_json"])
                        card = result.promise_card
                    else:
                        result = ActionResult.model_validate_json(row["result_json"])
                        card = result.updated_card
                    if card:
                        promise = get_promise(database, card.promise_id)
                        if promise is None or promise.channel_id not in adapter.enabled_channels:
                            continue
                        updates = {"promise_card" if row["kind"] in ("message", "owner_card") else "updated_card": promise.card()}
                        result = type(result).model_validate({**result.model_dump(), **updates})
                    elif row["channel_id"] not in adapter.enabled_channels:
                        continue
                    message_ts = adapter.deliver_result(row["channel_id"], row["target_ts"], row["kind"], result)
                    record_delivery(
                        database, adapter.workspace_id, row["event_id"], row["kind"], message_ts, now,
                    )
                check_reminders(database, adapter.send_owner_reminder, now, adapter.workspace_id, adapter.enabled_channels)
                for channel_id in adapter.enabled_channels:
                    if channel_id == "*":
                        continue
                    last_month = get_last_monthly_report(database, adapter.workspace_id, channel_id)
                    if last_month is None:
                        with database:
                            record_monthly_report(database, adapter.workspace_id, channel_id, current_month_key, now)
                    elif last_month != current_month_key:
                        attempts = get_monthly_report_attempts(
                            database, adapter.workspace_id, channel_id, last_month,
                        )
                        if attempts < 3:
                            stats = get_monthly_missed_deadline_stats(
                                database, adapter.workspace_id, channel_id, last_month, settings.default_timezone,
                            )
                            with database:
                                record_monthly_report_attempt(database, adapter.workspace_id, channel_id, last_month, now)
                            delivered = adapter.send_channel_leaderboard(channel_id, stats, is_monthly=True)
                            if delivered:
                                with database:
                                    record_monthly_report(database, adapter.workspace_id, channel_id, current_month_key, now)
                        else:
                            with database:
                                record_monthly_report(database, adapter.workspace_id, channel_id, current_month_key, now)

        adapter = SlackAdapter(
            bot_token=settings.slack_bot_token, app_token=settings.slack_app_token,
            process_event_fn=process, handle_action_fn=handle_action, enabled_channels=settings.enabled_channels,
            record_delivery_fn=acknowledge_delivery, record_reminder_fn=acknowledge_reminder, tick_fn=tick,
            stats_fn=get_stats, claim_stats_fn=claim_stats,
        )


        def shutdown(signum, frame) -> None:
            adapter.stop()

        signals_to_register = [s for s in (signal.SIGINT, getattr(signal, "SIGTERM", None)) if s is not None]
        previous_handlers = {}
        for signum in signals_to_register:
            try:
                previous_handlers[signum] = signal.signal(signum, shutdown)
            except (ValueError, OSError):
                pass
        try:
            adapter.start()
        finally:
            for signum, handler in previous_handlers.items():
                try:
                    signal.signal(signum, handler)
                except (ValueError, OSError):
                    pass


if __name__ == "__main__":
    main()
