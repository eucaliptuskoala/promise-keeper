"""Smoke test script for verifying Slack Socket Mode infrastructure and incoming message events.

Usage:
    .venv/bin/python smoke_test/main.py
"""

import logging
import os
import sys
from pathlib import Path
from dotenv import load_dotenv
from slack_bolt import App
try:
    from slack_bolt.adapter.socket_mode.websocket_client import SocketModeHandler
except ImportError:
    from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError

# Configure console logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("smoke_test")

# Load environment variables from repo root .env
project_root = Path(__file__).resolve().parent.parent
env_path = project_root / ".env"
if env_path.is_file():
    load_dotenv(dotenv_path=env_path)
    logger.info("Loaded configuration from %s", env_path)
else:
    load_dotenv()
    logger.warning("No .env file found at %s; relying on process environment", env_path)

bot_token = os.getenv("BOT_OAUTH_TOKEN") or os.getenv("SLACK_BOT_TOKEN")
app_token = os.getenv("BOT_APP_TOKEN") or os.getenv("SLACK_APP_TOKEN")

if not bot_token:
    logger.error("Missing Bot OAuth Token. Ensure BOT_OAUTH_TOKEN or SLACK_BOT_TOKEN is set in .env")
    sys.exit(1)

if not app_token:
    logger.error("Missing App Token. Ensure BOT_APP_TOKEN or SLACK_APP_TOKEN is set in .env")
    sys.exit(1)

# Initialize Bolt application
app = App(token=bot_token)


@app.event("message")
def handle_message(event, say, logger):
    """Handle incoming message events from channels, threads, or DMs."""
    subtype = event.get("subtype")
    user_id = event.get("user")
    channel_id = event.get("channel")
    text = event.get("text", "")
    ts = event.get("ts")
    thread_ts = event.get("thread_ts")
    bot_id = event.get("bot_id")

    # Filter out bot self-messages or system messages if present
    if bot_id:
        logger.info(
            "[Message from bot ignored] bot_id=%s channel=%s subtype=%s",
            bot_id,
            channel_id,
            subtype,
        )
        return

    if subtype:
        logger.info(
            "[Message subtype received] subtype=%s channel=%s user=%s ts=%s",
            subtype,
            channel_id,
            user_id,
            ts,
        )
        return

    # Regular human message
    is_threaded = thread_ts is not None and thread_ts != ts
    logger.info(
        "=== INCOMING MESSAGE ===\n"
        "  User:      %s\n"
        "  Channel:   %s\n"
        "  Thread:    %s\n"
        "  Timestamp: %s\n"
        "  Text:      %s\n"
        "========================",
        user_id,
        channel_id,
        thread_ts if is_threaded else "None (main channel)",
        ts,
        text,
    )


@app.event("app_mention")
def handle_mention(event, logger):
    """Handle explicit app mentions."""
    user_id = event.get("user")
    channel_id = event.get("channel")
    text = event.get("text", "")
    ts = event.get("ts")

    logger.info(
        "=== APP MENTION ===\n"
        "  User:      %s\n"
        "  Channel:   %s\n"
        "  Timestamp: %s\n"
        "  Text:      %s\n"
        "===================",
        user_id,
        channel_id,
        ts,
        text,
    )


def main():
    try:
        auth_response = app.client.auth_test()
        bot_user = auth_response.get("user")
        bot_user_id = auth_response.get("user_id")
        team_name = auth_response.get("team")
        team_id = auth_response.get("team_id")

        logger.info("Connected to Slack workspace '%s' (ID: %s)", team_name, team_id)
        logger.info("Bot authenticated as '%s' (User ID: %s)", bot_user, bot_user_id)
        logger.info("Starting Socket Mode listener... (Press Ctrl+C to stop)")

        handler = SocketModeHandler(app, app_token)
        handler.start()
    except SlackApiError as e:
        logger.error("Slack API error: %s", e.response.get("error", str(e)))
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Smoke test interrupted by user. Exiting.")


if __name__ == "__main__":
    main()
