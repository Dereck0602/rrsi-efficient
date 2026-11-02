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

Built-in Workers receive shared role instructions on each `ask` or `edit` call.
These are appended to the harness's native system instructions (Codex uses its
native developer instructions); Claude Code retains the `claude_code` preset.
The algorithm's request still defines the search strategy and reply format.
`ask` requests analysis or text, while `edit` requests a focused change in the
supplied working directory. The shared instructions require evidence-based,
reusable changes and honest validation reports, and reserve benchmark evaluation
and version selection for the framework unless explicitly delegated.

Ordinary Target `run` and `rollout` calls receive no Worker supplement. This is
behavioral guidance, not filesystem or process isolation, and does not change
tool permissions. The supplement is recorded in the call's `RunConfig`, not in
Snapshot resources. Snapshot-local runtimes from older versions or custom Agent
adapters need the corresponding system-instruction hook to support it; saved
Snapshots are not rewritten automatically.

## Upcoming adapters

| Harness | Status |
| --- | --- |
| OpenCode | Doing |

These are planned integrations and are not available CLI harness choices.

## Check the task environment

The harness needs the tools the task requires. Container execution also needs a compatible Linux environment and explicit model credentials. Benchmark data, simulators, Docker images, and graders are supplied by the benchmark integration.

See [harness / benchmark responsibilities](../benchmark-harness-boundary.md) and [Add a harness](../development/harness.md).
