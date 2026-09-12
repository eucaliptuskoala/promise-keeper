"""SQLite records, chronological history and recoverable outbound responses."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from promise_keeper.models import ActionResult, LeaderboardEntry, PipelineResult, PromiseRecord



def open_storage(path: str) -> sqlite3.Connection:
    """Open an existing database connection with WAL mode and foreign keys enabled."""
    database = sqlite3.connect(path, timeout=5)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA foreign_keys = ON")
    if path != ":memory:":
        database.execute("PRAGMA journal_mode = WAL")
    return database


def initialize_storage(path: str) -> sqlite3.Connection:
    """Open storage without replacing existing data. Caller closes the connection."""
    database = open_storage(path)
    database.executescript("""
        CREATE TABLE IF NOT EXISTS promises (
            promise_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            owner_id TEXT NOT NULL,
            record_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS promise_scope ON promises(workspace_id, channel_id, owner_id);
        CREATE INDEX IF NOT EXISTS promise_dependency ON promises(json_extract(record_json, '$.depends_on_promise_id'));
        CREATE TABLE IF NOT EXISTS promise_history (
            history_id INTEGER PRIMARY KEY,
            promise_id TEXT NOT NULL REFERENCES promises(promise_id),
            operation TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            recorded_at TEXT NOT NULL,
            previous_json TEXT,
            current_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS processed_events (
            workspace_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            actor_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            target_ts TEXT NOT NULL,
            result_json TEXT NOT NULL,
            delivery_state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            retry_at REAL NOT NULL,
            PRIMARY KEY(workspace_id, event_id, kind)
        );
        CREATE INDEX IF NOT EXISTS pending_deliveries ON processed_events(workspace_id, delivery_state, retry_at);
        CREATE TABLE IF NOT EXISTS delivered_cards (
            workspace_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            message_ts TEXT NOT NULL,
            promise_id TEXT NOT NULL REFERENCES promises(promise_id),
            PRIMARY KEY(workspace_id, channel_id, message_ts)
        );
        CREATE TABLE IF NOT EXISTS app_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stats_commands (
            workspace_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            channel_id TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            PRIMARY KEY(workspace_id, event_id)
        );
    """)
    return database



def get_promise(database: sqlite3.Connection, promise_id: str) -> PromiseRecord | None:
    row = database.execute("SELECT record_json FROM promises WHERE promise_id = ?", (promise_id,)).fetchone()
    return PromiseRecord.model_validate_json(row["record_json"]) if row else None


def list_open_promises(
    database: sqlite3.Connection, workspace_id: str, channel_id: str, owner_id: str, thread_ts: str,
) -> list[PromiseRecord]:
    rows = database.execute(
        "SELECT record_json FROM promises WHERE workspace_id = ? AND channel_id = ? AND owner_id = ?",
        (workspace_id, channel_id, owner_id),
    ).fetchall()
    promises = [PromiseRecord.model_validate_json(row["record_json"]) for row in rows]
    return sorted(
        (promise for promise in promises if promise.status in ("pending_confirmation", "waiting", "confirmed")),
        key=lambda promise: (promise.thread_ts == thread_ts, promise.updated_at),
        reverse=True,
    )[:20]


def list_thread_open_promises(
    database: sqlite3.Connection, workspace_id: str, channel_id: str, thread_ts: str,
    before: datetime | None = None,
) -> list[PromiseRecord]:
    """Retrieve eligible prerequisites, including completed promises, before the source event."""
    rows = database.execute(
        "SELECT record_json FROM promises WHERE workspace_id = ? AND channel_id = ?",
        (workspace_id, channel_id),
    ).fetchall()
    promises = [PromiseRecord.model_validate_json(row["record_json"]) for row in rows]
    return sorted(
        (
            promise for promise in promises
            if promise.thread_ts == thread_ts
            and promise.status in ("pending_confirmation", "waiting", "confirmed", "completed")
            and (before is None or promise.source_occurred_at < before)
        ),
        key=lambda promise: promise.source_occurred_at,
        reverse=True,
    )[:20]


def list_dependent_promises(
    database: sqlite3.Connection, prerequisite_promise_id: str,
) -> list[PromiseRecord]:
    """Retrieve all promises directly blocked by the given prerequisite promise."""
    rows = database.execute(
        "SELECT record_json FROM promises WHERE json_extract(record_json, '$.depends_on_promise_id') = ?",
        (prerequisite_promise_id,),
    ).fetchall()
    return [PromiseRecord.model_validate_json(row["record_json"]) for row in rows]


def list_delivered_cards(database: sqlite3.Connection, promise_id: str) -> list[sqlite3.Row]:
    """Return each Slack card currently associated with a promise."""
    return database.execute(
        "SELECT channel_id, message_ts FROM delivered_cards WHERE promise_id = ?",
        (promise_id,),
    ).fetchall()


def has_dependency_cycle(
    database: sqlite3.Connection, promise_id: str, candidate_dependency_id: str,
) -> bool:
    """Detect if setting candidate_dependency_id as a prerequisite creates a circular dependency."""
    if promise_id == candidate_dependency_id:
        return True
    visited: set[str] = set()
    current_id: str | None = candidate_dependency_id
    while current_id and current_id not in visited:
        visited.add(current_id)
        prerequisite = get_promise(database, current_id)
        if not prerequisite:
            break
        if prerequisite.depends_on_promise_id == promise_id:
            return True
        current_id = prerequisite.depends_on_promise_id
    return False

def save_promise(database: sqlite3.Connection, promise: PromiseRecord) -> None:
    """Participates in the caller's transaction."""
    database.execute(
        """INSERT INTO promises VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(promise_id) DO UPDATE SET record_json = excluded.record_json""",
        (promise.promise_id, promise.workspace_id, promise.channel_id, promise.owner_id, promise.model_dump_json()),
    )


def record_history(
    database: sqlite3.Connection, previous: PromiseRecord | None, current: PromiseRecord,
    operation: str, actor_id: str, event_id: str, occurred_at: datetime, recorded_at: datetime,
) -> None:
    database.execute(
        """INSERT INTO promise_history
           (promise_id, operation, actor_id, event_id, occurred_at, recorded_at, previous_json, current_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (current.promise_id, operation, actor_id, event_id, occurred_at.isoformat(), recorded_at.isoformat(),
         previous.model_dump_json() if previous else None, current.model_dump_json()),
    )


def get_processed_event(
    database: sqlite3.Connection, workspace_id: str, event_id: str, kind: str,
) -> sqlite3.Row | None:
    return database.execute(
        "SELECT * FROM processed_events WHERE workspace_id = ? AND event_id = ? AND kind = ?",
        (workspace_id, event_id, kind),
    ).fetchone()


def record_processed_event(
    database: sqlite3.Connection, workspace_id: str, event_id: str, kind: str, actor_id: str,
    channel_id: str, target_ts: str, result: PipelineResult | ActionResult, now: datetime,
) -> None:
    needs_delivery = result.should_respond if isinstance(result, PipelineResult) else result.success
    database.execute(
        "INSERT INTO processed_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (workspace_id, event_id, kind, actor_id, channel_id, target_ts, result.model_dump_json(),
         "pending" if needs_delivery else "sent", now.timestamp()),
    )
    card = result.promise_card if isinstance(result, PipelineResult) else result.updated_card
    if not needs_delivery or card is None or kind not in ("message", "action"):
        return
    destinations = database.execute(
        "SELECT channel_id, message_ts FROM delivered_cards WHERE workspace_id = ? AND promise_id = ?",
        (workspace_id, card.promise_id),
    ).fetchall()
    pending_owner = database.execute(
        """SELECT 1 FROM processed_events WHERE workspace_id = ? AND kind = 'owner_card'
           AND delivery_state = 'pending' AND json_extract(result_json, '$.promise_card.promise_id') = ?""",
        (workspace_id, card.promise_id),
    ).fetchone()
    has_owner_card = any(row["channel_id"] != (card.channel_id or channel_id) for row in destinations)
    if kind == "message" and not pending_owner and not has_owner_card:
        database.execute(
            "INSERT INTO processed_events VALUES (?, ?, 'owner_card', ?, ?, ?, ?, 'pending', 0, ?)",
            (workspace_id, event_id, actor_id, channel_id, target_ts, result.model_dump_json(), now.timestamp()),
        )
    update = ActionResult(success=True, updated_card=card)
    for destination in destinations:
        if kind == "action" and destination["channel_id"] == channel_id and destination["message_ts"] == target_ts:
            continue
        database.execute(
            "INSERT INTO processed_events VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
            (workspace_id, event_id, f"card_update:{destination['channel_id']}:{destination['message_ts']}",
             actor_id, destination["channel_id"], destination["message_ts"], update.model_dump_json(), now.timestamp()),
        )


def record_delivery(
    database: sqlite3.Connection, workspace_id: str, event_id: str, kind: str,
    message_ts: str | None, now: datetime,
) -> None:
    """None means failed delivery. Retry up to three times with persisted backoff."""
    row = get_processed_event(database, workspace_id, event_id, kind)
    if row is None or row["delivery_state"] == "sent":
        return
    result = json.loads(row["result_json"])
    card = result.get("promise_card") or result.get("updated_card")
    attempts = row["attempts"] + 1
    state = "sent" if message_ts else ("failed" if attempts >= 3 else "pending")
    with database:
        database.execute(
            """UPDATE processed_events SET delivery_state = ?, attempts = ?, retry_at = ?
               WHERE workspace_id = ? AND event_id = ? AND kind = ?""",
            (state, attempts, (now + timedelta(seconds=30 * attempts)).timestamp(), workspace_id, event_id, kind),
        )
        if message_ts and card and kind != "owner_card":
            bind_card(database, workspace_id, row["channel_id"], message_ts, card["promise_id"])


def bind_card(
    database: sqlite3.Connection, workspace_id: str, channel_id: str, message_ts: str, promise_id: str,
) -> None:
    database.execute(
        "INSERT OR REPLACE INTO delivered_cards VALUES (?, ?, ?, ?)",
        (workspace_id, channel_id, message_ts, promise_id),
    )


def is_delivered_card(
    database: sqlite3.Connection, workspace_id: str, channel_id: str, message_ts: str, promise_id: str,
) -> bool:
    return database.execute(
        """SELECT 1 FROM delivered_cards
           WHERE workspace_id = ? AND channel_id = ? AND message_ts = ? AND promise_id = ?""",
        (workspace_id, channel_id, message_ts, promise_id),
    ).fetchone() is not None


def pending_responses(database: sqlite3.Connection, workspace_id: str, now: datetime) -> list[sqlite3.Row]:
    return database.execute(
        """SELECT * FROM processed_events
           WHERE workspace_id = ? AND delivery_state = 'pending' AND retry_at <= ?
           ORDER BY retry_at LIMIT 20""",
        (workspace_id, now.timestamp()),
    ).fetchall()


def claim_stats_command(
    database: sqlite3.Connection, workspace_id: str, event_id: str, channel_id: str, now: datetime,
) -> bool:
    """Claim a Slack stats event before posting, preventing duplicate replies on event replay."""
    cursor = database.execute(
        "INSERT OR IGNORE INTO stats_commands VALUES (?, ?, ?, ?)",
        (workspace_id, event_id, channel_id, now.isoformat()),
    )
    return cursor.rowcount == 1


def get_app_metadata(database: sqlite3.Connection, key: str) -> str | None:
    row = database.execute("SELECT value FROM app_metadata WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_app_metadata(database: sqlite3.Connection, key: str, value: str, now: datetime) -> None:
    database.execute(
        """INSERT INTO app_metadata (key, value, updated_at) VALUES (?, ?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at""",
        (key, value, now.isoformat()),
    )


def get_last_monthly_report(database: sqlite3.Connection, workspace_id: str, channel_id: str) -> str | None:
    return get_app_metadata(database, f"monthly_report:{workspace_id}:{channel_id}")


def record_monthly_report(
    database: sqlite3.Connection, workspace_id: str, channel_id: str, month_key: str, now: datetime,
) -> None:
    set_app_metadata(database, f"monthly_report:{workspace_id}:{channel_id}", month_key, now)


def get_monthly_report_attempts(
    database: sqlite3.Connection, workspace_id: str, channel_id: str, month_key: str,
) -> int:
    value = get_app_metadata(database, f"monthly_report_attempts:{workspace_id}:{channel_id}:{month_key}")
    return int(value) if value is not None else 0


def record_monthly_report_attempt(
    database: sqlite3.Connection, workspace_id: str, channel_id: str, month_key: str, now: datetime,
) -> None:
    attempts = get_monthly_report_attempts(database, workspace_id, channel_id, month_key) + 1
    set_app_metadata(
        database,
        f"monthly_report_attempts:{workspace_id}:{channel_id}:{month_key}",
        str(attempts),
        now,
    )


def _month_bounds(month_key: str, timezone_name: str = "UTC") -> tuple[datetime, datetime]:
    tz = timezone.utc if timezone_name == "UTC" else ZoneInfo(timezone_name)
    year, month = (int(part) for part in month_key.split("-", maxsplit=1))
    start = datetime(year, month, 1, tzinfo=tz).astimezone(timezone.utc)
    end = (
        datetime(year + 1, 1, 1, tzinfo=tz) if month == 12 else datetime(year, month + 1, 1, tzinfo=tz)
    ).astimezone(timezone.utc)
    return start, end


def _aggregate_stats(promises: list[PromiseRecord], now: datetime) -> list[LeaderboardEntry]:
    overdue_by_owner: dict[str, list[PromiseRecord]] = {}
    for promise in promises:
        overdue_by_owner.setdefault(promise.owner_id, []).append(promise)
    leaderboard = []
    for owner_id, items in overdue_by_owner.items():
        sorted_items = sorted(items, key=lambda promise: promise.deadline_at or now)
        leaderboard.append({
            "owner_id": owner_id,
            "overdue_count": len(items),
            "sample_actions": [promise.action for promise in sorted_items[:3]],
            "oldest_deadline_at": sorted_items[0].deadline_at,
        })
    leaderboard.sort(key=lambda entry: (-entry["overdue_count"], entry["oldest_deadline_at"] or now))
    return leaderboard


def _channel_promises(
    database: sqlite3.Connection, workspace_id: str, channel_id: str,
) -> list[PromiseRecord]:
    rows = database.execute(
        "SELECT record_json FROM promises WHERE workspace_id = ? AND channel_id = ?",
        (workspace_id, channel_id),
    ).fetchall()
    return [PromiseRecord.model_validate_json(row["record_json"]) for row in rows]


def get_unfulfilled_stats(
    database: sqlite3.Connection, workspace_id: str, channel_id: str, now: datetime,
) -> list[LeaderboardEntry]:
    """Aggregate unfulfilled commitments (confirmed/waiting with deadline_at < now) grouped by owner."""
    now_utc = now.astimezone(timezone.utc)
    return _aggregate_stats(
        [
            promise for promise in _channel_promises(database, workspace_id, channel_id)
            if promise.status in ("confirmed", "waiting")
            and promise.deadline_at is not None
            and promise.deadline_at < now_utc
        ],
        now_utc,
    )


def get_monthly_missed_deadline_stats(
    database: sqlite3.Connection, workspace_id: str, channel_id: str, month_key: str,
    timezone_name: str = "UTC",
) -> list[LeaderboardEntry]:
    """Aggregate commitments missed in one calendar month, including late completions."""
    start, end = _month_bounds(month_key, timezone_name)
    missed = []
    for promise in _channel_promises(database, workspace_id, channel_id):
        if promise.deadline_at is None:
            continue
        completed_late = (
            promise.status == "completed"
            and promise.deadline_at < promise.last_event_at
            and start <= promise.last_event_at < end
        )
        still_overdue = (
            promise.status in ("confirmed", "waiting")
            and start <= promise.deadline_at < end
        )
        if completed_late or still_overdue:
            missed.append(promise)
    return _aggregate_stats(missed, end)

