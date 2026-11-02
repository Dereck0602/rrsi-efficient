# Configuration & CLI

Start from an existing YAML example, then override the fields you need. Run commands from the repository root so relative data and output paths resolve consistently.

## The configuration blocks

```yaml
run:
  output_dir: ../runs
agent:
  name: miniagent
  max_turns: 6
  llm:
    model_name: gpt-5-mini
    api_key: null
    base_url: null
algorithm:
  name: skillopt
  num_epochs: 1
  max_steps: 1
  batch_size: 2
  reflection_minibatch_size: 2
  edit_budget: 1
  worker:
    type: llm
    llm:
      model_name: gpt-5-mini
      api_key: null
      base_url: null
benchmark:
  name: searchqa
  data_path: ./examples/data/quickstart.jsonl
  split:
    train: 0.5
    dev: 0.25
    test: 0.25
  seed: 42
```

This is the [bundled quickstart](../../examples/quickstart.yaml). Method-specific fields belong under `algorithm`; benchmark-specific fields belong under `benchmark`.

| Block / field | What it controls |
| --- | --- |
| `run.output_dir` | Parent directory for experiment records. |
| `run.mode`, `run.snapshot` | Saved-Snapshot evaluation when selected through configuration. See [evaluation](./evaluation.md). |
| `agent.name` | Target harness: `miniagent`, `claude_code`, `codex`, `pi`, `hermes`, `openclaw`, or `opencode`. |
| `agent.llm.*` | Target model, credentials, endpoint, and supported request options. |
| `agent.max_turns` | Target execution budget, subject to harness support. |
| `agent.source_dir` | Alternate resource source for a harness. |
| `algorithm.name` | Registered RSI method. |
| `algorithm.worker.type` | `llm` for proposals, or `agent` for a native harness Worker (selected with `name`). |
| `algorithm.worker.llm.*` | Worker's independent model and connection settings. |
| `benchmark.name`, `data_path` | Benchmark adapter and optional local data. |
| `benchmark.split`, `split_counts`, `seed` | Experimental sampling and split policy supported by the adapter. |

See [algorithm settings](../integrations/algorithms.md) and the [benchmark configuration index](../benchmark-quickstart.md) for fields specific to a method or task.

## Configure Target and Worker separately

The YAML loader reads literal values. Expand environment variables in the shell when passing credentials:

```bash
flashrsi run --config examples/quickstart.yaml \
  --model "$TARGET_MODEL" \
  --worker-model "$WORKER_MODEL" \
  --set agent.llm.api_key="$TARGET_API_KEY" \
  --set algorithm.worker.llm.api_key="$WORKER_API_KEY" \
  --set agent.llm.base_url="$TARGET_BASE_URL" \
  --set algorithm.worker.llm.base_url="$WORKER_BASE_URL"
```

For example, `--agent.llm.api_key "$TARGET_API_KEY"` is equivalent to its `--set` form. Overrides are parsed as YAML values, including numbers, booleans, lists, and `null`. Saved configuration records redact known credentials.

Judge models, user simulators, and postprocessing models have their own configuration where required. Changing the Target does not configure these roles. See the [launcher role guide](../experiment-launcher.zh-CN.md#模型配置与文件位置).

## Pi and Hermes context settings

Both harnesses execute Python code from the selected Snapshot, without an installed upstream CLI or npm packages. Configure these fields on the selected role (`agent` or `algorithm.worker`); keep model connections under that role's `llm`.

| Harness | Fields and behavior |
| --- | --- |
| Pi | `compaction.enabled` defaults to `true`; `reserveTokens` and `keepRecentTokens` default to 16384 and 20000. Supports threshold/overflow compaction and `/compact [instructions]`. `context_window` and `model_max_tokens` override the pinned model catalog. |
| Pi session | `session_file` selects a Pi v3 JSONL conversation; `session_leaf_id` resumes a branch. `/tree ENTRY_ID` selects a history node, with `branch_summary` controlling the abandoned branch summary. |
| Hermes | `compression.enabled` defaults to `true`, with `threshold: 0.50`. Optional `proactive_prune_tokens`, `micro_compact` and `idle_compact_after_seconds` enable separate maintenance strategies. `/compress [focus]` requests manual compaction. |
| Hermes session | `session_file` saves messages, todos and compression state. `compression.checkpoint_required: true` requires a durable local checkpoint before a lossy rewrite and suppresses micro/provider-native compaction. |
| Hermes Responses | Set `llm.model_api: openai-responses` for Responses. `compression.codex_responses_native: true` opts into provider-native compaction only when the upstream model, endpoint and capability checks permit it. |

Session files are opt-in runtime state outside the Snapshot. Use a separate file for each independent benchmark task. The [integration guide](../agent-integration.md) describes supported paths and validation boundaries; fixed-response parity checks do not establish live-model accuracy.

## CLI options

### Run evolution

```bash
flashrsi run --config examples/quickstart.yaml [OPTIONS]
```

| Option | Meaning |
| --- | --- |
| `--config PATH`, `-c PATH` | Read a YAML configuration. |
| `--algorithm NAME`, `--algo NAME` | Select the RSI method that proposes and evaluates candidate edits. See [algorithm integrations](../integrations/algorithms.md). |
| `--agent-harness NAME`, `--agent NAME` | Select the Target harness to evolve and execute. See [harness integrations](../agent-integration.md). |
| `--benchmark NAME` | Select the task suite and scoring protocol. See the [benchmark configuration index](../benchmark-quickstart.md). |
| `--model NAME` | Set the Target model used to solve benchmark tasks. |
| `--worker-model NAME` | Set the independent Worker model used to propose edits. |
| `--output-dir PATH` | Choose the parent directory for run records and artifacts. |
| `--set PATH=VALUE` | Override any dotted configuration field, including `algorithm.worker.type` and `algorithm.worker.name`. Repeat as needed. |
| `--path.to.key VALUE` | Alternative dotted-field syntax. |
| `--check-only` | Check startup without model calls; may download data. |
| `--prepare-only` | Create a redacted configuration directory without checking runtime readiness. |
| `--help`, `-h` | Print current CLI help. |

### Evaluate a Snapshot

```bash
flashrsi evaluate --config examples/evaluate_hotpotqa.yaml \
  --snapshot /path/to/snapshot.json [OPTIONS]
```

`evaluate` shares `--config`, `--agent-harness` (or the compatibility alias
`--agent`), `--benchmark`, `--model`, `--output-dir`, `--set`, dotted overrides,
and `--check-only`. It requires `--snapshot PATH` and does not use the
evolution-only options.

## Change the experiment

To switch the quickstart from skill learning to instruction search:

```bash
flashrsi run --config examples/opro.yaml \
  --agent-harness miniagent \
  --benchmark searchqa \
  --set benchmark.data_path=./examples/data/quickstart.jsonl
```

Supply each role's model and credentials as above. Prefer the destination algorithm's example so its Worker type, budgets, and parameters stay together. Registration alone does not imply that every algorithm, harness, and benchmark combination is compatible.

For programmatic usage, see the [Python API](../usage.md#python-api).
