# Promise Keeper

Promise Keeper is a Slack-native agent that helps teams follow through on promises made in everyday conversation.

## Why it exists

“I'll send it tomorrow” often disappears into a busy thread. Other people may be waiting on that delivery, but nobody creates a task or remembers to follow up. Promise Keeper turns those agreements into trackable commitments without requiring manual task entry.

## Planned capabilities

Detect real commitments rather than vague intentions or unaccepted requests. Understand short replies from conversation context and extract the owner, action, deadline when stated, and source message.

Keep promises and their chronological history across restarts. Recognize later completion or deadline changes, and let owners confirm, dismiss, complete, or adjust promises through Slack controls.

Follow up once on confirmed promises that become overdue.

After the core lifecycle works end to end, track one simple explicit dependency and notify the waiting owner when the blocking delivery is confirmed complete.

Provide a simulation mode where Alice, Bob, and Andrii use the same processing pipeline as real Slack messages.

## Example

```text
Alice: I'll send the designs by noon.
Bob: Once Alice sends them, I'll build the page by 3 pm.
Andrii: I might look into animations.
```

The required workflow should track Alice's promise, ask her to confirm it, preserve it across restart, and later complete or remind it while ignoring Andrii's vague intention. Linking Bob's dependency and notifying him after Alice finishes is a stretch workflow.

## Running

The new MVP is not runnable yet. Installation and startup commands will be added when its entry points are implemented and verified.

The planned local setup is one Python application using Slack Bolt with Socket Mode. Slack mode requires an installed Slack app, bot and app-level tokens, and model API access. The target model is `qwen3.8-27b` through Aptget, but authenticated completion and tool behavior have not yet been verified. Offline simulation tests will not require Slack accounts or API keys.

## Status

Early implementation stage. Model configuration exists, but the capabilities above and the Slack integration are not yet verified. Slack is the initial platform.

See [PLAN.md](PLAN.md) for the implementation plan and [AGENTS.md](AGENTS.md) for coding-agent guidelines.
