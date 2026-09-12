"""Promise Keeper application entry point."""

import logging
import signal
import sys
from dotenv import load_dotenv

from promise_keeper.adapters.slack import SlackAdapter
from promise_keeper.config import load_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("promise_keeper")


def main() -> None:
    """Start the Promise Keeper application with the Slack Socket Mode adapter."""
    load_dotenv()
    try:
        settings = load_settings()
    except Exception:
        # Fall back to Slack transport mode if model API key is not yet configured
        try:
            settings = load_settings(require_model=False)
            logger.warning(
                "OPENAI_API_KEY not configured. Running in Slack transport mode. "
                "Set OPENAI_API_KEY in .env when model access is available."
            )
        except Exception as exc:
            logger.error("Failed to load settings: %s", exc)
            sys.exit(1)

    if not settings.slack_bot_token or not settings.slack_app_token:
        logger.error(
            "Missing Slack configuration. Ensure SLACK_BOT_TOKEN (or BOT_OAUTH_TOKEN) "
            "and SLACK_APP_TOKEN (or BOT_APP_TOKEN) are set in .env"
        )
        sys.exit(1)

    logger.info("Initializing Promise Keeper with model %s", settings.openai_model)
    adapter = SlackAdapter(
        bot_token=settings.slack_bot_token,
        app_token=settings.slack_app_token,
    )

    def shutdown(signum, frame):
        logger.info("Received signal %s. Shutting down gracefully...", signum)
        adapter.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    logger.info("Starting Socket Mode adapter. Listening for Slack events...")
    try:
        adapter.start()
    except KeyboardInterrupt:
        shutdown(signal.SIGINT, None)


if __name__ == "__main__":
    main()
