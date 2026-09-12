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

For Slack, configure `OPENAI_API_KEY`, `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, and comma-separated `SLACK_ENABLED_CHANNELS` in environment variables or local `.env`. Existing `BOT_OAUTH_TOKEN` and `BOT_APP_TOKEN` names are accepted. Invite the bot to enabled public channels, enable Socket Mode and interactivity, subscribe to `message.channels`, and grant bot scopes `channels:history`, `chat:write`, `im:write`, plus app-level `connections:write`.

`OPENAI_BASE_URL` defaults to `https://api.aptget.nl/v1`; `OPENAI_MODEL` defaults to `qwen3.8-27b`. The OpenAI SDK requests one native function call per message, executes it through the core, then returns the actual result to the model. There is no JSON-mode, regex or fake-key fallback. Cards reflect SQLite state, not model prose. `DATABASE_PATH` defaults to `promise_keeper.db` in the working directory; `APP_TIMEZONE` defaults to `UTC`. Windows named timezones use `tzdata`. `MODEL_TIMEOUT_SECONDS` defaults to 20 per request and accepts values greater than 0 and at most 60; an event uses at most two model requests, without SDK retries.

```powershell
.\.venv\Scripts\python.exe -m promise_keeper
.\.venv\Scripts\python.exe -m promise_keeper --simulation --live-model
```

Authenticated model generation and native function calling remain unverified because no model key was available. The gateway must support Chat Completions function tools, strict schemas, `tool_choice` and `parallel_tool_calls`. Real Slack delivery is also unverified. Check an ordinary message without mentioning the bot, actual owner/non-owner clicks, a deadline change and private reminder delivery before relying on Slack mode.

## Data and limits

Selected messages, bounded context and relevant commitments go to the configured model provider; its retention policy is not established here. SQLite stores normalized agreements and effects, not full conversation context. Environment files and conventional database/sidecar paths are excluded from Git. Existing databases are never automatically reset.

The application must remain running for reminders and retries. Private reminders normally send once until snooze or reschedule. Outbound failures have at most three attempts; a crash after external delivery can still cause duplicates. Bot-token access to older full thread history is limited; fallback retrieves the parent and uses bounded in-memory context. Message edits/deletions, multiple processes sharing a database and dependencies are outside this MVP. See [PLAN.md](PLAN.md) for the exact contract, data flow and recovery limits.
