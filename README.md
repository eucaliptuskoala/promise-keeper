# Promise Keeper

Promise Keeper is a Slack-native agent that helps teams follow through on promises made in everyday conversation.

## Why it exists

“I'll send it tomorrow” often disappears into a busy thread. Other people may be waiting on that delivery, but nobody creates a task or remembers to follow up. Promise Keeper turns those agreements into trackable commitments without requiring manual task entry.

## Planned capabilities

Detect real commitments rather than vague intentions or unaccepted requests. Understand short replies from conversation context and extract the owner, action, deadline when stated, and source message.

Keep promises and their chronological history across restarts. Recognize later completion or deadline changes, and let owners confirm, dismiss, complete, or adjust promises through Slack controls.

Follow up on unfinished promises and track simple explicit dependencies: who is waiting on whom. Notify the next person when a blocking delivery is confirmed complete.

Provide a simulation mode where Alice, Bob, and Andrii use the same processing pipeline as real Slack messages.

## Example

```text
Alice: I'll send the designs by noon.
Bob: Once Alice sends them, I'll build the page by 3 pm.
Andrii: I might look into animations.
```

The agent should track Alice's and Bob's promises, link the dependency after confirmation, and ignore Andrii's vague intention. When Alice confirms delivery, it should notify Bob that he can start.

## Running

The new MVP is not runnable yet. Installation and startup commands will be added when its entry points are implemented and verified.

The planned local setup is one Python application using Slack Bolt with Socket Mode. Slack mode requires an installed Slack app, bot and app-level tokens, and model API access. Offline simulation tests will not require Slack accounts or API keys.

## Status

Planning stage. The capabilities above are implementation targets, not verified features. Slack is the initial platform.

See [PLAN.md](PLAN.md) for the implementation plan and [AGENTS.md](AGENTS.md) for coding-agent guidelines.
