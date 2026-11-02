# OpenCode native Python runtime

This directory is the FlashRSI Snapshot boundary for the embedded OpenCode turn
path. It is pure Python and does not import Bun, Node, npm, the OpenCode CLI, or
host OpenCode state. `run()` is the Target entry point.

Each turn loads Snapshot/workspace resources, restores an append-only SQLite
session, compacts context, calls the configured model transport, executes tool
calls in order, records receipts, and continues until a final response,
cancellation, timeout, or step budget. Provider adapters preserve the
Chat Completions, Responses, and Anthropic Messages tool envelopes.

The tool registry follows OpenCode's coding surface: `read`, `write`, `edit`,
`apply_patch`, `bash`, `glob`, `grep`, `webfetch`, `diagnostics`, `task`, `todowrite`, and `skill`. `exec`,
`process`, `apply_patch`, `read_skill`, and `memory_search` remain explicit
compatibility tools for evolved FlashRSI snapshots. `task` accepts an injected
Python callback; permission values `ask` are treated as denied because the
embedded Target has no interactive approval channel; without one it returns a structured `not_configured` receipt
instead of contacting a host service.

The TUI, server, account/OAuth flows, MCP and LSP supervisors, and remote
credential discovery are outside the benchmark Target. The exact upstream
release and this adaptation boundary are recorded in `PROVENANCE.json`.
