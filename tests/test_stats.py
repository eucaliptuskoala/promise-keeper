"""Tests for unfulfilled commitments stats, leaderboard card, and monthly reporting."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from promise_keeper.adapters.slack import SlackAdapter, build_leaderboard_card
from promise_keeper.models import AgentDecision, NormalizedEvent, PromiseRecord
from promise_keeper.storage import (
    get_last_monthly_report,
    get_unfulfilled_stats,
    initialize_storage,
    record_monthly_report,
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
    # Empty stats
    empty_blocks = build_leaderboard_card([])
    assert "Все молодцы!" in empty_blocks[2]["text"]["text"]

    # Populated stats
    stats = [
        {"owner_id": "U1", "overdue_count": 3, "sample_actions": ["Fix bug", "Write test"]},
        {"owner_id": "U2", "overdue_count": 1, "sample_actions": ["Deploy"]},
    ]
    card_blocks = build_leaderboard_card(stats, is_monthly=False)
    text = card_blocks[2]["text"]["text"]
    assert "🥇 *1.* <@U1> — *3* просроченных обещания" in text
    assert "«Fix bug»" in text
    assert "🥈 *2.* <@U2> — *1* просроченное обещание" in text


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


def test_send_channel_leaderboard() -> None:
    bot = MagicMock()
    bot.client.auth_test.return_value = {"user_id": "UBOT", "team_id": "T1"}
    bot.client.chat_postMessage.return_value = {"ts": "999.0001"}

    adapter = SlackAdapter.__new__(SlackAdapter)
    adapter.app = bot
    adapter.bot_user_id = "UBOT"
    adapter.workspace_id = "T1"
    adapter.enabled_channels = ("C1",)

    # Disabled channel returns None
    assert adapter.send_channel_leaderboard("C_DISABLED", []) is None
    bot.client.chat_postMessage.assert_not_called()

    # Enabled channel posts leaderboard
    ts = adapter.send_channel_leaderboard("C1", [{"owner_id": "alice", "overdue_count": 1, "sample_actions": ["Task"]}], is_monthly=True)
    assert ts == "999.0001"
    bot.client.chat_postMessage.assert_called_once()
