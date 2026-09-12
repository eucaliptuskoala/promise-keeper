# Promise Keeper MVP implementation plan

## Goal and scope

Build a new Python-first agent that tracks commitments made in Slack, maintains their chronological history, follows up on open promises, and handles one simple explicit dependency between promises.

The first target is one workspace and explicitly enabled public channels. Slack is the demo platform; Teams is not an initial deliverable. The agent tracks promised work but does not perform it.

Build the new application from scratch. Do not import the previous research prototype's source, tests, or prompts. This document is a plan, not evidence that any integration or feature already works.

## Architecture

Use one Python application for direct Slack connectivity, promise interpretation, tools, storage, authorization, lifecycle rules, reminder decisions, and simulation. Use Slack Bolt with Socket Mode, Pydantic for boundary validation, SQLite for local persistence, and pytest for offline tests.

No messaging middleware, Node.js runtime, or agent-to-runtime HTTP bridge is needed. Use native Slack Block Kit for promise cards and interactive controls. Keep the Slack adapter thin and all promise rules in the shared Python core.

```text
Slack ↔ Python Slack Bolt adapter (Socket Mode / Web API)
                         ↕
             normalize → shared pipeline ← synthetic events
                         ↕
                   agent + tools ↔ model API
                         ↕
                       SQLite

Python reminder check → Slack Web API / captured test output
```

Socket Mode receives Slack events and interactive payloads without a public HTTP endpoint. Outbound messages use the Slack Web API. Both paths belong to the same Python application; the reminder check is an in-process job, not another service.

OpenAI supplies reasoning and function calling. Use its Python SDK with a small bounded tool loop or a documented Python runner if one is already chosen. Verify model/tool behavior before depending on it; do not invent package names, API signatures, or compatibility claims.

Keep the model configurable rather than hard-coding one. Model API credits are separate from Codex usage credits. Use only a provider configuration whose required API and tool behavior have been checked.

References: [Slack Socket Mode](https://docs.slack.dev/apis/events-api/using-socket-mode/), [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling).

## Slack integration and first feasibility check

Create a Slack app in the test workspace, enable Socket Mode, and obtain a bot token and an app-level token with the connection permission. Keep them in local environment configuration as SLACK_BOT_TOKEN and SLACK_APP_TOKEN. Keep model credentials in the same local configuration, never in source control.

Install the app in the workspace and invite it to the enabled public channel. Subscribe to message.channels so ordinary human messages reach the adapter without mentioning the bot. Define a minimal manifest with permissions needed for public-channel messages, posting, and owner DMs. Reinstall the app when changed scopes require it.

Enable interactivity and handle native Block Kit actions through Bolt. Acknowledge interactive actions promptly before slow model work. Keep message callbacks short and perform expensive processing outside the transport callback.

The application can run locally while connected to the internet; no public tunnel or deployment is required for testing. It must remain running to receive events and send reminders. A valid token or socket connection does not prove end-to-end behavior.

First verify an ordinary, non-mentioned human message reaching the Python pipeline and a reply returning to the same Slack thread. Check source IDs, original timestamps, and access to the bounded thread/channel context. Do not require a mention to detect each promise.

Next verify an actual owner button click, a delayed outbound message, and private owner delivery through the Web API. Keep the public-channel allowlist explicit, ignore bot output, and avoid subscribing to inbound DMs unless that scope is intentionally added.

Document failed checks and missing permissions rather than silently replacing passive monitoring with a mention-only flow. Native cards, context retrieval, and reminder delivery require separate live checks.

Reference: [public-channel message events](https://docs.slack.dev/reference/events/message.channels/).

## Shared input and processing pipeline

Normalize inbound events into stable workspace, channel, actor, message, thread, and event IDs, original event time, received time, text, and source reference. Obtain identity from trusted transport metadata.

Ignore the application's own output, bot messages, unsupported subtypes, and already processed events. Keep edited/deleted-message reconciliation outside the first MVP and document that limitation.

For a supported new message, collect bounded recent thread/channel context in chronological order and relevant stored promises. Supply the source timestamp and configured timezone so “tomorrow” is interpreted relative to the message, not the processing date.

The agent interprets the input, may call scoped tools, and returns an acknowledgement or clarification only when useful. Application code validates writes and records resulting outbound effects. Ordinary discussion should not trigger a public bot response.

Serialize processing within a conversation for the first version. Preserve source order where available and detect late events; do not blindly apply an older deadline change over a newer confirmed update.

## Agent and tool flow

```text
message + context + relevant promises
                  ↓
            model reasoning
                  ↓
            typed tool call
                  ↓
 scope / owner / state validation
                  ↓
        SQLite transaction
                  ↓
     actual tool result to model
                  ↓
       response or clarification
```

Start with this small tool surface:

| Tool | Purpose |
| --- | --- |
| list_open_promises | Read relevant open promises in the trusted event scope. |
| create_promise | Store a grounded commitment with its source evidence. |
| complete_promise | Close a clearly matched promise under the owner policy. |
| reschedule_promise | Change an existing promise's deadline with authorization. |
| link_dependency | Propose or confirm an explicit link between existing promises. |

Create no obligation from an unaccepted request. Distinguish a firm commitment from a hope, hypothetical, quotation, joke, or vague intention. Use context for “yes, tomorrow,” but clarify when the promised action remains unclear.

Keep unstated deadlines null and retain original deadline wording. Do not guess material timezone or date ambiguity.

A clear detection can be stored as pending confirmation and shown with confirm/dismiss controls. Deadline nudges apply only after confirmation. Owner-authored, unambiguous completion and deadline updates can be acknowledged and applied; ambiguous matches or changes proposed by someone else require owner confirmation.

Explicit confirm, done, dismiss, snooze, and deadline-change actions are deterministic handlers. Verify the acting user against the promise owner; button payloads alone are not authorization. Snooze changes notification timing, not the agreed deadline.

Use finite timeouts, bounded retries, and a maximum tool-loop length. Treat Slack content as untrusted data. The model must not choose arbitrary SQL, bypass scope checks, or route notifications to unrestricted recipients.

## Storage and chronological memory

Use ordinary SQLite records and transactions, not a vector database or full event-sourcing framework. Suggested minimal records:

| Record | Contents |
| --- | --- |
| Promise | ID, workspace/channel/thread, owner, action, source message, original deadline text, parsed UTC deadline, confirmation/lifecycle state, timestamps. |
| Promise history | Promise ID, action, actor, source reference, event and recorded times, relevant previous/new values. |
| Dependency | Downstream/upstream promise IDs, evidence, proposed/confirmed state. |
| Processed event | Stable event key and processing state for duplicate suppression and recoverable failures. |
| Notification | Promise/dependency reference, notification type, authorized destination, unique logical key, delivery state and timestamps. |

Keep blocked state separate from open/completed lifecycle status. A closed or dismissed promise stays available as evidence but is excluded from future reminders.

Write state and corresponding history together. A deadline update preserves the previous agreement in history. Event deduplication prevents duplicate records and history entries; recoverable failures must not be marked permanently successful before processing finishes.

Memory has two levels: bounded recent conversation context and persistent promise records/history. Important older promises are retrieved from storage even after their original messages leave the context window. Do not archive or resend the whole workspace.

For example, creation on September 12, deadline change on September 13, and completion on September 14 belong to one promise, not three unrelated tasks. Retain both source time and recorded time to explain late delivery.

Keep synthetic and real databases isolated. Do not delete a real database to reset a demo.

## Reminders and dependency-aware handoffs

Run a simple periodic reminder check inside the Python application. A controlled clock supplies “now” during tests. Select confirmed, open promises whose deadlines have passed and whose notification policy allows a follow-up.

For the first version, send one overdue nudge and allow an explicit snooze to schedule a later nudge. Exclude completed, dismissed, unconfirmed, and deadline-free promises. Recheck state before sending. Persist notification state across restarts.

Prefer private owner reminders after verifying the delivery path. If private delivery fails, record a bounded retry; do not silently expose the promise in a public fallback.

Keep dependency support small: one upstream promise per downstream promise in the same authorized channel scope. Require explicit evidence and downstream-owner confirmation. Reject self-links and cycles.

An upstream delay flags the downstream work as blocked. Surface the upstream blocker rather than sending an independent generic nudge about the downstream task. Do not alter any downstream deadline automatically.

Confirmed upstream completion clears the corresponding blocker and produces one handoff notification. If downstream deadlines no longer make sense, propose a change and wait for those owners' agreement.

Store delivery intent and results so successful notifications are normally suppressed on retry. A crash between external delivery and saving the result can still cause a duplicate; do not claim exactly-once delivery. Reminder service is active only while the application runs.

## Testing and simulation

Synthetic Alice, Bob, and Andrii messages enter the same normalized Python pipeline, with distinct stable IDs and chronological timestamps. Advance the clock rather than sleeping. Capture outbound effects and never connect the simulator to a real Slack workspace.

Offline tests use scripted model/tool responses. They verify state, tool execution, validation, history, and scheduling without keys. A separate opt-in live-model simulation evaluates language understanding. Transport integration tests verify Slack event normalization, Bolt action handling, and real platform behavior separately.

Core scenarios cover a firm promise, vague intention, unaccepted request, missing deadline, relative deadline, contextual reply, completion, deadline update, dismissal, and snooze.

Reliability checks cover duplicate events, unauthorized controls, late messages, restart persistence, invalid model output, tool failures, timeouts, injection attempts, and failed outbound delivery.

The dependency demo covers a proposed/confirmed link, upstream delay, blocked downstream work, upstream completion, one handoff notification, and an unchanged downstream deadline. Test self-link and cycle rejection.

Claim real Slack success only after a human message reaches Python and produces the intended Slack response. Verify controls with actual clicks and delayed notifications with an actual timer check. Keep mocked, live-model, and live-Slack results separate.

## Hackathon implementation order

Assume two people and a four-hour build window. Validate direct Slack connectivity alongside the offline core; prioritize a complete workflow over extra infrastructure.

| Time | Python/core work | Integration/demo work | Checkpoint |
| --- | --- | --- | --- |
| 0:00–0:30 | Scaffold fresh Python project; define normalized events and minimal persistence. | Configure the Slack app and prove Socket Mode → Python → Slack; probe passive messages and delayed sends. | Python path verified; integration risks recorded. |
| 0:30–1:30 | Implement create/list/complete flow, history, scripted tests, and live-model simulation. | Verify context and owner controls; prepare realistic synthetic conversation. | One end-to-end promise lifecycle works. |
| 1:30–2:30 | Add confirmed-promise reminders, deduplication, and restart checks. | Connect cards/actions to the same Python core; exercise real Slack flow. | Actual message, owner click, and overdue nudge work. |
| 2:30–3:15 | Add the single explicit dependency, blocker, and handoff. | Test the three-person scenario and fallback/error behavior. | Dependency scenario works without rewriting deadlines. |
| 3:15–4:00 | Freeze features; fix blockers and run checks. | Update verified run instructions, rehearse, record demo, and prepare submission. | Reproducible demo and honest documentation. |

If time slips, preserve the working promise lifecycle and reminders before expanding dependencies. Do not add other platforms or integrations. Mark omitted features as planned and distinguish passive monitoring from any temporary mention-triggered flow.

## Documentation and data boundaries

README remains a short project introduction with verified startup commands when available. This plan holds technical sequencing and decisions. AGENTS contains durable coding principles rather than the current backlog.

Document the direct data path: Slack events reach our Python application, and selected reasoning context is sent to the model provider. Retain only necessary evidence and avoid full-conversation logs. Keep credentials, local databases, and real workspace fixtures out of Git.

Before handing off, record important implementation choices, actual run commands, required permissions, successful checks, and unresolved limitations. Do not present this plan as implemented functionality.
