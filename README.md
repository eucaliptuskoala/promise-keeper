# Promise Keeper

Promise Keeper is a Slack-native commitment tracker that turns everyday channel conversations into accountable agreements. When team members make promises in chat, the bot detects them, confirms them with the owner, tracks deadlines and dependencies, delivers private reminders, and publishes accountability leaderboards.

## Features and functionality

### Commitment detection and interpretation

Promise Keeper listens to messages in authorized public Slack channels. Using an LLM function call (compatible with OpenAI-style endpoints), the bot analyzes natural-language messages to detect firm, self-made commitments:

- **Personal commitments**: Extracts the proposed action, original evidence text, and explicit or relative deadlines (for example, "I will send the designs by noon tomorrow").
- **Smart filtering**: Ignores casual chatter, hypotheticals, jokes, offers of ability ("I can help"), and unaccepted requests.
- **Clarification**: Prompts with a concise question if a commitment or update is materially ambiguous.
- **Natural language updates**: The commitment owner can complete or reschedule commitments directly in conversation threads (for example, "Finished the designs" or "Moving deadline to Friday").

### Two-tier card system: public visibility and private controls

- **Public channel cards**: When a commitment is created, a read-only card appears in the channel thread. It displays the commitment action, owner, status, and deadline formatted in each viewer's local timezone. Public cards contain no interactive buttons.
- **Private direct message controls**: Interactive controls (Confirm, Dismiss, Complete, Reschedule) are delivered directly to the owner in a private DM with the bot. This prevents accidental or unauthorized tampering by other users. Actions in DM automatically synchronize and update both the private card and the public channel card.

### Commitment lifecycle

1. **Pending confirmation**: Detected commitments begin in a `pending_confirmation` state. The owner can click **Confirm** to activate tracking or **Dismiss** to discard false positives.
2. **Waiting on dependencies**: Commitments that depend on another task enter a `waiting` state until that prerequisite is fulfilled.
3. **Confirmed / In progress**: Active commitments eligible for reminders as deadlines approach.
4. **Reschedule**: The owner can adjust the deadline at any time using an interactive date and time picker modal or natural language.
5. **Complete**: When work is done, the owner marks it completed via the DM button or natural language.

### Prerequisites and task dependencies

Team commitments often depend on prior work (for example, "I'll deploy staging once Alice finishes the backend"):

- The bot links the dependent commitment to the candidate prerequisite.
- The dependent commitment remains in a `waiting` status and will not trigger premature reminders.
- Once the prerequisite promise is marked complete, the dependent commitment is unblocked automatically, and any relative timeframe (for example, "within 2 days after that") begins from that completion moment.

### Private reminders and snooze

- When a deadline arrives or passes, the bot sends an overdue reminder directly to the owner via direct message.
- Reminders include a direct permalink to the original message in the channel and display the source channel name.
- The reminder card includes a **Snooze** button, which postpones notifications by one hour without changing the underlying deadline, alongside **Complete** and **Reschedule** options.

### Accountability stats and leaderboards

- **On-demand channel stats ("Wall of Shame")**: Team members can type `!stats` or mention `@Promise Keeper stats` in any enabled channel. The bot replies with a leaderboard ranking members with overdue commitments, their overdue count, and sample unfulfilled actions.
- **Automated monthly reports**: At the start of each month, the background runner compiles missed deadlines from the previous calendar month and posts a summary report to the channel.

## Running locally

Python 3.12 or newer is required.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate   # On Windows: .venv\Scripts\activate
pip install -e '.[dev]'
```

### Offline simulation and testing

Verify the entire core workflow offline without Slack credentials or live model keys:

```bash
python -m promise_keeper --simulation
pytest -q
```

The offline simulation verifies Alice, Bob, and Andrii through promise creation, owner confirmation, SQLite persistence, private reminder capture, and completion using deterministic scripted model decisions.

To test against your configured model provider with synthetic data (no Slack delivery):

```bash
python -m promise_keeper --simulation --live-model
```

### Running the Slack bot

```bash
python -m promise_keeper
```

## Configuration

Configure settings via environment variables or a local `.env` file:

| Variable | Description | Default |
| --- | --- | --- |
| `OPENAI_API_KEY` | API key for the model provider | Required for live bot and `--live-model` |
| `OPENAI_BASE_URL` | Base URL for OpenAI-compatible endpoint | `https://api.aptget.nl/v1` |
| `OPENAI_MODEL` | Model name | `qwen3.8-27b` |
| `MODEL_TIMEOUT_SECONDS` | Timeout in seconds for model requests (1–60) | `20` (60 recommended for larger models) |
| `SLACK_BOT_TOKEN` | Slack Bot User OAuth Token (`xoxb-...`) | Required for Slack |
| `SLACK_APP_TOKEN` | Slack App-Level Token (`xapp-...`) | Required for Slack |
| `SLACK_ENABLED_CHANNELS` | Comma-separated channel IDs or `*` for all public channels | Required for Slack |
| `DATABASE_PATH` | Path to SQLite database | `promise_keeper.db` |
| `APP_TIMEZONE` | Fallback timezone for relative dates | `UTC` |

### Slack setup requirements

1. Enable **Socket Mode** and **Interactivity** in your Slack App configuration.
2. Under **Event Subscriptions**, subscribe to the `message.channels` bot event.
3. Under **OAuth & Permissions**, grant the following Bot Token Scopes:
   - `channels:read`
   - `channels:history`
   - `chat:write`
   - `im:write`
4. Under App-Level Tokens, grant the `connections:write` scope.
5. Invite the bot to the designated public channels.

## Data and limits

- **Model context**: Only inbound messages, bounded thread context (up to 20 messages), and open commitment summaries are sent to the model provider.
- **Storage**: SQLite stores normalized agreements, event receipts, delivery state, and history snapshots. Full conversation histories are never retained in the database.
- **Delivery reliability**: Outbound Slack deliveries are tracked with up to three automatic retry attempts. Background reminders and retries run in-process on a periodic tick.
- Technical architecture, schema details, and state machine contracts are documented in [PLAN.md](PLAN.md).
