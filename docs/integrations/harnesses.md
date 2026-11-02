# Harnesses

A harness executes the agent's instructions, tools, skills, and runtime logic. Its FlashRSI adapter declares which resources belong in a Snapshot, then runs the selected resource version through its execution entry point.

## Available integrations

| Harness | Editable resources | Runtime and details |
| --- | --- | --- |
| MiniAgent | Instructions, skills, tools, configuration, declared Python code | Direct mapping of a small native Python harness. [Usage](../usage.md#native-harnesses) · [source](../../flashrsi/agents/miniagent) |
| Claude Code | Instructions, skills, commands, agents, hooks, MCP/LSP registrations, settings | Official Agent SDK (`claude-agent-sdk>=0.2.153,<0.3`); exposed resource interfaces are mapped to files. [Adapter details](../agent-integration.md) |
| Codex | Python core, instructions, tools, skills, agents, hooks, plugins, memory, rules/config files | Snapshot-local Python semantic port based on `rust-v0.147.0`; runs in an isolated process. [Guide](../codex.md) · [version and migration boundary](../codex-core-extraction.md) |
| Pi | Pure-Python Pi-compatible runtime, instructions, skills, prompt templates, settings | Snapshot-local `runtime/pi_native` implements the headless Pi v1.4.0 path and emits Pi-shaped JSON events; no Node/npm dependency. [Adapter details](../agent-integration.md) |
| Hermes | Python runtime, SOUL instructions, skills and tool settings | Snapshot-local headless Hermes v0.22.5 subset, retaining upstream Python policies and local tool helpers. [Adapter details and limits](../agent-integration.md) · [example](../../examples/hermes.yaml) |
| OpenClaw | Pure-Python Snapshot runtime, bootstrap files, skills, tools, policy, session and memory settings | Snapshot-local `runtime/openclaw_native` follows the pinned embedded turn path across Chat Completions, Responses and Anthropic Messages; no Node/npm/Gateway dependency. [Adapter details](../agent-integration.md) · [example](../../examples/openclaw.yaml) |
| OpenCode | Pure-Python Snapshot runtime, instructions, coding tools, permissions, sessions and settings | Snapshot-local `runtime/opencode_native` follows OpenCode v1.19.3's embedded prompt/processor path and provider tool envelopes; no Bun/Node/CLI dependency. [Adapter details](../agent-integration.md) · [example](../../examples/opencode.yaml) |

Codex's Python core implements the documented paths from a pinned Rust baseline. It does not launch the Codex CLI, fall back to the host core, or claim complete equivalence to every upstream feature. For example, declaring rules as resources does not implement rules-based execution policy; see [capability limits](../codex.md#明确的能力边界).

## Target and Agent Worker

The same adapter can solve benchmark tasks as a Target or edit candidate files as a Worker. Each role receives its own configuration and model connection:

```yaml
agent:
  name: miniagent
  llm:
    model_name: gpt-5-mini
algorithm:
  worker:
    type: agent
    name: claude_code
    llm:
      model_name: your-worker-model
```

This fragment only illustrates role selection. Keep the rest of the destination method's example and supply both roles' credentials and endpoints.

OpenCode is available as a Target and Agent Worker. Its Snapshot excludes the Bun/Node host CLI, TUI,
server, OAuth account flows, MCP supervisor, and LSP process supervisor; benchmark execution uses the
shared Agent/container boundary.

## Check the task environment

The harness needs the tools the task requires. Container execution also needs a compatible Linux environment and explicit model credentials. Benchmark data, simulators, Docker images, and graders are supplied by the benchmark integration.

See [harness / benchmark responsibilities](../benchmark-harness-boundary.md) and [Add a harness](../development/harness.md).
