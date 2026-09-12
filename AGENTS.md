# Coding-agent guidelines

## Working approach

Use Python as the primary language. Prefer one application with direct, well-supported integrations over additional runtimes, middleware, or services. Do not duplicate business rules across languages or transports.

Optimize for a useful, demonstrable hackathon MVP. Prefer the smallest complete end-to-end workflow over a broad collection of unfinished features. Reliability, clarity, and a reproducible demo matter more than production-scale infrastructure.

Read relevant source, tests, configuration, and Markdown documentation before changing code. Use established project patterns and helpers first. Keep changes focused and preserve unrelated user work.

Avoid speculative abstractions, additional services, orchestration layers, and packages. Introduce complexity only for a demonstrated need. Ask before making a material architectural change or adding an abstraction or dependency beyond the agreed plan.

## Processing and reasoning

Keep business processing independent of the messaging transport. Real messages and synthetic test input must share the same core pipeline. Adapters normalize input and deliver output; they must not implement their own competing business logic.

Separate deterministic application logic from LLM reasoning. Authorization, validation, persistence, scheduling, explicit user actions, and duplicate suppression belong in code. Use the model for language interpretation and decisions that genuinely need context.

Expose narrow, typed tools. Application code must validate and execute tool calls and return actual results. Never treat model-generated prose as evidence that an action succeeded.

Derive identity and permissions from trusted application context. Treat messages, documents, and model output as untrusted input, not instructions that can override security boundaries.

Preserve event identity and time semantics. Keep missing information explicit rather than inventing it. Bound context, retries, model latency, and tool execution.

## Verification

Test changes in proportion to their risk. Use isolated data and deterministic clocks where time matters. Keep offline tests independent of external services, and make live API or platform checks explicit.

Mocked tests verify application behavior, not model quality or external delivery. A running process, valid credentials, or a connected transport does not prove a complete user workflow works.

Report what was changed, what was checked, what failed, and what remains unverified. Do not claim capabilities, compatibility, privacy guarantees, or successful integrations without evidence. Do not silently substitute mocked behavior for a live check.

## Privacy and security

Minimize collected, stored, logged, and provider-bound data. Document external data flows and retention behavior. Keep access scoped to authorized users and conversations.

Never put credentials, private conversation data, local environment files, or real databases in version control. Use safe placeholders and synthetic fixtures. Do not request secrets in chat or print them in logs.

Require appropriate authorization before side effects. Do not broaden recipients, permissions, or data access as an implicit fallback after a failure.

Protect existing data. Explain the consequences before destructive operations and prefer recoverable alternatives. Do not delete or replace user data merely to make a test pass.

## Documentation and collaboration

Keep README concise and user-facing. Put technical sequencing and design detail in the implementation plan. Keep these coding guidelines independent of temporary tasks and implementation status.

Document significant decisions and tradeoffs near the affected code or in the plan. Keep documentation consistent with actual behavior, verified commands, configuration, and external-service requirements.

Communicate concisely and directly. Do not use bullet points unless requested. Raise concrete blockers and material scope changes rather than silently making a different product.

Do not commit, push, stage unrelated files, or rewrite Git history without explicit authorization.
