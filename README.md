# Promise Keeper

Promise Keeper tracks commitments in ordinary Slack messages. An owner confirms each detected promise before reminders become eligible.

The core supports creation, dismissal, completion, deadline changes, and reminder snooze. SQLite preserves agreements, history, event receipts and delivery state. Promise dependencies remain future work.

## Run locally

Python 3.12 or newer is required:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
.\.venv\Scripts\python.exe -m promise_keeper --simulation
.\.venv\Scripts\python.exe -m pytest -q
```

The verified offline simulation runs Alice, Bob and Andrii through the real core using labelled scripted model decisions and a temporary synthetic database. It checks creation, confirmation, persistence after reopening SQLite, one captured private reminder and completion. It does not evaluate model understanding or deliver to Slack.

For Slack, configure `OPENAI_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, and `SLACK_ENABLED_CHANNELS` in environment variables or local `.env`. Use `SLACK_ENABLED_CHANNELS=*` for all active public channels, or comma-separated channel IDs for a selected list. Public channels are resolved at startup; restart after creating a channel. Existing `BOT_OAUTH_TOKEN` and `BOT_APP_TOKEN` names are accepted. Invite the bot to the public channels where it should receive messages, enable Socket Mode and interactivity, subscribe to `message.channels`, and grant bot scopes `channels:read`, `channels:history`, `chat:write`, `im:write`, plus app-level `connections:write`.

`OPENAI_BASE_URL` defaults to `https://api.aptget.nl/v1`; `OPENAI_MODEL` defaults to `qwen3.8-27b`. The OpenAI SDK requests one native function call per message and executes it through the core. The actual result goes directly to the adapter, without another model request. There is no JSON-mode, regex or fake-key fallback. Cards reflect SQLite state, not model prose. `DATABASE_PATH` defaults to `promise_keeper.db` in the working directory; `APP_TIMEZONE` defaults to `UTC`. Windows named timezones use `tzdata`. `MODEL_TIMEOUT_SECONDS` defaults to 20 and accepts values greater than 0 and at most 60; an event uses at most one model request, without SDK retries.

```powershell
.\.venv\Scripts\python.exe -m promise_keeper
.\.venv\Scripts\python.exe -m promise_keeper --simulation --live-model
```

The live-model simulation passed against the configured gateway with a 60-second timeout; creation took about 29 seconds, exceeding the default 20. The example environment sets `MODEL_TIMEOUT_SECONDS=60`. Bot authentication, public-channel listing and app-token Socket Mode access also passed. A real Slack message produced a card, and a private overdue reminder was delivered after adjusting one existing promise for the test. Actual owner clicks verified confirmation, dismissal, deadline-modal saving and private completion on clearly labelled seeded test cards. Snooze on the existing promise deferred its reminder by one hour without changing the deadline, and delivery resumed after accelerating that wait for the test. Non-owner controls still need a live check.

Public cards show the promise and its status without buttons. The owner receives controls in a private conversation with the bot; actions update both private and public cards. Cards show the original deadline wording followed by its exact date and time, localized by Slack to the viewer's device timezone. Private controls and reminders also show the source channel and a link to the original message. If link lookup fails, private delivery still proceeds with the channel reference.

## Data and limits

Selected messages, bounded context and relevant commitments go to the configured model provider; its retention policy is not established here. SQLite stores normalized agreements and effects, not full conversation context. Environment files and conventional database/sidecar paths are excluded from Git. Existing databases are never automatically reset.

The application must remain running for reminders and retries. Private reminders normally send once until snooze or reschedule. Outbound failures have at most three attempts; a crash after external delivery can still cause duplicates. Bot-token access to older full thread history is limited; fallback retrieves the parent and uses bounded in-memory context. Message edits/deletions, multiple processes sharing a database and dependencies are outside this MVP. See [PLAN.md](PLAN.md) for the exact contract, data flow and recovery limits.
