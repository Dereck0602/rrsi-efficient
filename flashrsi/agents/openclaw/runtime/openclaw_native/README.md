# OpenClaw native Python runtime

This directory is the FlashRSI Snapshot boundary for the OpenClaw embedded agent
turn. It is a pure Python implementation and has no Node, npm, Gateway, or host
OpenClaw CLI dependency. `run()` is the only entry point used by the Target.

The turn follows the same observable order as the pinned OpenClaw runtime:

1. load bootstrap files and the eligible skill catalog;
2. restore the append-only session transcript and append the new user turn;
3. compact context at the configured limit;
4. stream one provider response (Chat Completions, Responses, or Anthropic
   Messages);
5. execute every tool call in order and append its receipt;
6. continue until a final assistant response, cancellation, timeout, or the
   iteration budget.

Provider adapters preserve the protocol-specific tool envelopes: one assistant
`tool_calls` message for Chat Completions, `tool_use`/`tool_result` blocks for
Anthropic, and `function_call`/`function_call_output` items for Responses.
Sessions, skill manifests, memory hits, tool receipts, and runtime events are
deterministic plain Python data, so a Snapshot can be replayed without host
state.

The upstream Gateway, channels, UI, cron daemon, plugin marketplace, and
container sandbox supervisor are intentionally outside a benchmark Target. The
native policy still exposes OpenClaw's sandbox boundary as an explicit option;
the default is `sandbox.enabled: false`, matching OpenClaw's `sandbox.mode: off`.

The exact upstream baseline and the implemented boundary are recorded in
`PROVENANCE.json`. Changes to this directory are the runtime implementation that
actually executes; the reference OpenClaw installation is never imported.
