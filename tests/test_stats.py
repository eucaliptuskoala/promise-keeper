"""Tests for unfulfilled commitments stats, leaderboard card, and monthly reporting."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from promise_keeper.adapters.slack import SlackAdapter, build_leaderboard_card
from promise_keeper.models import AgentDecision, NormalizedEvent, PromiseRecord
from promise_keeper.storage import (
    claim_stats_command,
    get_last_monthly_report,
    get_monthly_missed_deadline_stats,
    get_monthly_report_attempts,
    get_unfulfilled_stats,
    initialize_storage,
    record_monthly_report,
    record_monthly_report_attempt,
    save_promise,
)


@pytest.fixture
def database(tmp_path: Path):
    connection = initialize_storage(str(tmp_path / "test_stats.db"))
    yield connection
    connection.close()


def test_get_unfulfilled_stats(database) -> None:
    now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
    past = now - timedelta(hours=2)
    future = now + timedelta(hours=2)

    # 1. Alice has 2 overdue confirmed promises
    p1 = PromiseRecord(
        promise_id="p-1", workspace_id="T1", channel_id="C1", thread_ts="100.1", owner_id="alice",
        action="Write API tests", deadline_text="2 hours ago", deadline_at=past, status="confirmed",
        source_message_id="100.1", source_occurred_at=past, created_at=past, updated_at=past, last_event_at=past,
    )
    p2 = PromiseRecord(
        promise_id="p-2", workspace_id="T1", channel_id="C1", thread_ts="100.1", owner_id="alice",
        action="Deploy staging", deadline_text="1 hour ago", deadline_at=past + timedelta(hours=1), status="confirmed",
        source_message_id="100.1", source_occurred_at=past, created_at=past, updated_at=past, last_event_at=past,
    )

    # 2. Bob has 1 overdue waiting promise
    p3 = PromiseRecord(
        promise_id="p-3", workspace_id="T1", channel_id="C1", thread_ts="100.1", owner_id="bob",
        action="Design UI mocks", deadline_text="2 hours ago", deadline_at=past, status="waiting",
        source_message_id="100.1", source_occurred_at=past, created_at=past, updated_at=past, last_event_at=past,
    )

    # 3. Charlie has 1 completed promise (should NOT count as overdue)
    p4 = PromiseRecord(
        promise_id="p-4", workspace_id="T1", channel_id="C1", thread_ts="100.1", owner_id="charlie",
        action="Fix CSS", deadline_text="2 hours ago", deadline_at=past, status="completed",
        source_message_id="100.1", source_occurred_at=past, created_at=past, updated_at=past, last_event_at=past,
    )

    # 4. David has 1 promise with future deadline (should NOT count as overdue)
    p5 = PromiseRecord(
        promise_id="p-5", workspace_id="T1", channel_id="C1", thread_ts="100.1", owner_id="david",
        action="Review PR", deadline_text="in 2 hours", deadline_at=future, status="confirmed",
        source_message_id="100.1", source_occurred_at=past, created_at=past, updated_at=past, last_event_at=past,
    )

    # 5. Eve has 1 overdue promise in a different channel C2
    p6 = PromiseRecord(
        promise_id="p-6", workspace_id="T1", channel_id="C2", thread_ts="200.1", owner_id="eve",
        action="Write docs", deadline_text="2 hours ago", deadline_at=past, status="confirmed",
        source_message_id="200.1", source_occurred_at=past, created_at=past, updated_at=past, last_event_at=past,
    )

    for p in (p1, p2, p3, p4, p5, p6):
        save_promise(database, p)

    # Query for channel C1
    stats_c1 = get_unfulfilled_stats(database, "T1", "C1", now)
    assert len(stats_c1) == 2
    # Alice is #1 with 2 overdue
    assert stats_c1[0]["owner_id"] == "alice"
    assert stats_c1[0]["overdue_count"] == 2
    assert "Write API tests" in stats_c1[0]["sample_actions"]
    # Bob is #2 with 1 overdue
    assert stats_c1[1]["owner_id"] == "bob"
    assert stats_c1[1]["overdue_count"] == 1


def test_monthly_metadata_tracking(database) -> None:
    now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
    assert get_last_monthly_report(database, "T1", "C1") is None

    record_monthly_report(database, "T1", "C1", "2026-09", now)
    assert get_last_monthly_report(database, "T1", "C1") == "2026-09"

    record_monthly_report(database, "T1", "C1", "2026-10", now)
    assert get_last_monthly_report(database, "T1", "C1") == "2026-10"


def test_build_leaderboard_card() -> None:
    # 1. Empty stats
    empty_card = build_leaderboard_card([])
    assert "Great job, everyone!" in empty_card[2]["text"]["text"]
    assert "Wall of Shame" in empty_card[0]["text"]["text"]

    # 2. Monthly empty report
    monthly_empty = build_leaderboard_card([], is_monthly=True)
    assert "Monthly Missed Deadlines Report" in monthly_empty[0]["text"]["text"]

    # 3. Populated stats
    stats = [
        {"owner_id": "U1", "overdue_count": 3, "sample_actions": ["Fix bug", "Write test"], "oldest_deadline_at": None},
        {"owner_id": "U2", "overdue_count": 1, "sample_actions": ["Deploy"], "oldest_deadline_at": None},
    ]
    card = build_leaderboard_card(stats, is_monthly=False)
    text = card[2]["text"]["text"]
    assert "🥇 *1.* <@U1> — *3* overdue commitments" in text
    assert '"Fix bug"' in text
    assert "🥈 *2.* <@U2> — *1* overdue commitment" in text


def test_monthly_stats_include_late_completion_in_report_month(database) -> None:
    deadline = datetime(2026, 8, 31, 18, tzinfo=timezone.utc)
    completed_at = datetime(2026, 9, 2, 10, tzinfo=timezone.utc)
    late = PromiseRecord(
        promise_id="late", workspace_id="T1", channel_id="C1", thread_ts="100.1", owner_id="alice",
        action="Ship API", deadline_text="August 31", deadline_at=deadline, status="completed",
        source_message_id="100.1", source_occurred_at=deadline - timedelta(days=1),
        created_at=deadline - timedelta(days=1), updated_at=completed_at, last_event_at=completed_at,
    )
    save_promise(database, late)

    stats = get_monthly_missed_deadline_stats(database, "T1", "C1", "2026-09")

    assert stats[0]["owner_id"] == "alice"
    assert stats[0]["overdue_count"] == 1


def test_monthly_missed_deadline_stats_with_custom_timezone(database) -> None:
    # Deadline on Sept 30 at 23:30 in New York (UTC-4 in Sep) -> Oct 1 03:30 UTC
    ny_deadline = datetime(2026, 10, 1, 3, 30, tzinfo=timezone.utc)
    promise = PromiseRecord(
        promise_id="ny-promise", workspace_id="T1", channel_id="C1", thread_ts="100.1", owner_id="alice",
        action="Finish report", deadline_text="End of September", deadline_at=ny_deadline, status="confirmed",
        source_message_id="100.1", source_occurred_at=ny_deadline - timedelta(days=5),
        created_at=ny_deadline - timedelta(days=5), updated_at=ny_deadline - timedelta(days=5),
        last_event_at=ny_deadline - timedelta(days=5),
    )
    save_promise(database, promise)

    # In UTC, Oct 1 03:30 is outside September (2026-09)
    stats_utc = get_monthly_missed_deadline_stats(database, "T1", "C1", "2026-09", timezone_name="UTC")
    assert len(stats_utc) == 0

    # In America/New_York, it is Sept 30 23:30, so inside September (2026-09)
    stats_ny = get_monthly_missed_deadline_stats(database, "T1", "C1", "2026-09", timezone_name="America/New_York")
    assert len(stats_ny) == 1
    assert stats_ny[0]["owner_id"] == "alice"


def test_monthly_report_attempts_are_bounded_in_storage(database) -> None:
    now = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)

    assert get_monthly_report_attempts(database, "T1", "C1", "2026-08") == 0
    record_monthly_report_attempt(database, "T1", "C1", "2026-08", now)

    assert get_monthly_report_attempts(database, "T1", "C1", "2026-08") == 1


def test_manual_stats_trigger() -> None:
    bot = MagicMock()
    bot.client.auth_test.return_value = {"user_id": "UBOT", "team_id": "T1"}
    stats_mock = MagicMock(return_value=[{"owner_id": "alice", "overdue_count": 2, "sample_actions": ["Task 1"]}])
    process_mock = MagicMock()

    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter.app = bot
    adapter.bot_user_id = "UBOT"
    adapter.workspace_id = "T1"
    adapter.enabled_channels = ("C1",)
    adapter.stats_fn = stats_mock
    adapter.claim_stats_fn = MagicMock(return_value=True)
    adapter.format_leaderboard_fn = None
    adapter.process_event_fn = process_mock

    # 1. Mention bot with stats: <@UBOT> stats
    adapter._handle_inbound_message({"user": "bob", "channel": "C1", "text": "<@UBOT> stats", "ts": "100.1"})
    stats_mock.assert_called_once_with("T1", "C1")
    bot.client.chat_postMessage.assert_called_once()
    process_mock.assert_not_called()

    # 2. Text containing @Promise Keeper stats
    stats_mock.reset_mock()
    bot.client.chat_postMessage.reset_mock()
    adapter._handle_inbound_message({"user": "bob", "channel": "C1", "text": "hey @Promise Keeper stats please", "ts": "100.2"})
    stats_mock.assert_called_once_with("T1", "C1")
    bot.client.chat_postMessage.assert_called_once()
    process_mock.assert_not_called()

    # 3. Text starting with !stats
    stats_mock.reset_mock()
    bot.client.chat_postMessage.reset_mock()
    adapter._handle_inbound_message({"user": "bob", "channel": "C1", "text": "!stats", "ts": "100.3"})
    stats_mock.assert_called_once_with("T1", "C1")
    bot.client.chat_postMessage.assert_called_once()
    process_mock.assert_not_called()


def test_replayed_stats_command_does_not_post_twice() -> None:
    bot = MagicMock()
    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter.app = bot
    adapter.bot_user_id = "UBOT"
    adapter.workspace_id = "T1"
    adapter.enabled_channels = ("C1",)
    adapter.stats_fn = MagicMock(return_value=[])
    adapter.claim_stats_fn = MagicMock(side_effect=[True, False])
    adapter.format_leaderboard_fn = None

    event = {"user": "bob", "channel": "C1", "text": "!stats", "ts": "100.3"}
    adapter._handle_inbound_message(event, "Ev1")
    adapter._handle_inbound_message(event, "Ev1")

    bot.client.chat_postMessage.assert_called_once()


def test_claim_stats_command_is_idempotent(database) -> None:
    assert claim_stats_command(database, "T1", "Ev1", "C1", datetime(2026, 9, 12, tzinfo=timezone.utc))
    assert not claim_stats_command(database, "T1", "Ev1", "C1", datetime(2026, 9, 12, tzinfo=timezone.utc))


def test_send_channel_leaderboard() -> None:
    bot = MagicMock()
    bot.client.auth_test.return_value = {"user_id": "UBOT", "team_id": "T1"}
    bot.client.chat_postMessage.return_value = {"ts": "999.0001"}

    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter.app = bot
    adapter.bot_user_id = "UBOT"
    adapter.workspace_id = "T1"
    adapter.enabled_channels = ("C1",)
    adapter.format_leaderboard_fn = None

    # Disabled channel returns None
    assert adapter.send_channel_leaderboard("C_DISABLED", []) is None
    bot.client.chat_postMessage.assert_not_called()

    # Enabled channel posts English leaderboard
    ts = adapter.send_channel_leaderboard("C1", [{"owner_id": "alice", "overdue_count": 1, "sample_actions": ["Task"], "oldest_deadline_at": None}], is_monthly=True)
    assert ts == "999.0001"
    assert bot.client.chat_postMessage.call_args[1]["text"] == "Monthly Missed Deadlines Report"
    assert "Monthly Missed Deadlines Report" in bot.client.chat_postMessage.call_args[1]["blocks"][0]["text"]["text"]

    # Regular leaderboard
    ts_reg = adapter.send_channel_leaderboard("C1", [{"owner_id": "alice", "overdue_count": 1, "sample_actions": ["Task"], "oldest_deadline_at": None}], is_monthly=False)
    assert ts_reg == "999.0001"
    assert bot.client.chat_postMessage.call_args[1]["text"] == "Wall of Shame (Overdue Commitments)"
    assert "Wall of Shame" in bot.client.chat_postMessage.call_args[1]["blocks"][0]["text"]["text"]


def test_commitment_mentioning_stats_is_not_treated_as_stats_command() -> None:
    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter.bot_user_id = "UBOT"
    assert adapter.is_stats_command("!stats") is True
    assert adapter.is_stats_command("<@UBOT> stats") is True
    assert adapter.is_stats_command("@Promise Keeper stats please") is True
    # Commitments mentioning stats should NOT be treated as stats command
    assert adapter.is_stats_command("@Promise Keeper I will update stats tomorrow") is False
    assert adapter.is_stats_command("<@UBOT> I'll send the stats by noon") is False
    assert adapter.is_stats_command("@Promise Keeper I promise to fix the stats table") is False


def test_open_storage_sets_wal_mode(tmp_path: Path) -> None:
    from promise_keeper.storage import open_storage

    db_path = str(tmp_path / "wal_test.db")
    initialize_storage(db_path).close()

    conn = open_storage(db_path)
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode.lower() == "wal"
