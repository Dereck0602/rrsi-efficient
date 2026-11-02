# Choose and run an experiment

[README](../README.md) · [Benchmark configurations](benchmark-quickstart.md) · [Launcher guide](experiment-launcher.zh-CN.md)

FlashRSI registers 13 algorithms, 30 benchmark suites, and seven harnesses: MiniAgent, Claude Code, Codex, Pi, Hermes, OpenClaw, and OpenCode. Versions, years, and task domains within one adapter count as one suite. Registration does not mean every combination has completed a real model run.

## Start with a small run

Use [quickstart_launch.yaml](../examples/quickstart_launch.yaml) to run SkillOpt with MiniAgent on 12 bundled QA questions. Fill in the Target and Worker model settings, then run from the repository root:

```bash
python scripts/run_experiment.py --config examples/quickstart_launch.yaml
```

The launcher creates an isolated environment, installs the selected dependencies, and prepares data. Add `--plan` to inspect requirements or `--check-only` to prepare dependencies and data without model calls. These checks do not validate remote model authentication.

Start with Linux/WSL and Python 3.12. Git, a Docker Linux daemon, and Docker Compose are required where the chosen benchmark uses them. Codex runs the selected Snapshot Python core and needs a Responses-compatible endpoint; see [Codex setup](codex.md).

## Choose an algorithm and harness

Start from an [algorithm configuration](../README.md#supported-algorithms) so its parameters and Worker type stay together.

| What to improve | Algorithms | Worker |
| --- | --- | --- |
| Instructions | OPRO, GEPA, EvoPrompt, ACE | LLM or Agent |
| Skills | SkillOpt | LLM or Agent |
| Skills discovered by the native harness | Evo-Harness | Agent |
| Declared harness resources | AHE, ADAS, DarwinX, Meta-Harness, ModularRSI, RRSI, Arbor | Agent |

The Target solves tasks; the Worker proposes changes. Configure their models and connections independently in `agent.llm` and `algorithm.worker.llm`. Judge, user-simulator, and postprocessing models also use their own settings; see [role configuration](experiment-launcher.zh-CN.md#模型配置与文件位置).

The editable scope follows the [resources declared by each harness](../README.md#supported-harnesses). The harness must also provide the tools needed by the task.

- Evo-Harness needs native directory-based skill discovery and loading; see its [implementation notes](evo-harness.md).
- ModularRSI needs module file mappings. It can evolve on the benchmark's train split; train task IDs must be unique and disjoint from dev/test. Separate evolution data is optional. See [module and data configuration](modular-rsi.md) and the [Claude Code extension example](../examples/modular_rsi_claude_code.yaml).

Other method notes: [ACE](ace.md), [AHE](ahe.md), [ADAS](adas.md), [DarwinX](darwinx.md), [Meta-Harness](meta-harness.md), [RRSI](rrsi.md), and [Arbor](arbor.md).

## Choose a benchmark

The [30-suite configuration index](benchmark-quickstart.md) links every registered benchmark to a YAML example and its data requirements. Keep the example's grading and environment settings when changing the algorithm or harness.

| Execution | Benchmarks and preparation | Setup |
| --- | --- | --- |
| Text, search, and document tasks | Prepare the dataset; search tasks need web services. Some tasks also need a Judge, document corpus, or postprocessing model. | [Per-benchmark requirements](benchmark-quickstart.md), [web services](web-research-services.md) |
| Native Docker integrations | SWE-bench Verified uses its official verifier; AppWorld and BFCL use their own Docker integrations. | [Coding setups](coding-working-benchmarks.md), [launcher prerequisites](experiment-launcher.zh-CN.md#准备与检查) |
| Harbor | Terminal-Bench 2.0/2.1, SWE-bench Pro, DeepSWE, JobBench, and SpreadsheetBench 2 need Docker and Compose. JobBench also needs a Judge. | [Coding and spreadsheet tasks](coding-working-benchmarks.md), [DeepSWE](deepswe.md) |
| Native interactive environments | τ²/τ³ need their user simulator; τ² retail also needs a Judge. ALFWorld needs local native data and Python 3.11/3.12. | [τ²](tau2-bench.md), [ALFWorld](alfworld.md), [configuration index](benchmark-quickstart.md) |

SpreadsheetBench 2 covers its first three task categories and excludes Visualization. DeepSWE currently has an interface adapter; official task downloads and real execution are unverified. Each setup guide states its supported track and metric.

Container execution has additional harness requirements. Claude Code needs explicit credentials and cannot use host login; Claude Code and Codex containers reject `cli_path`. Codex containers execute the selected Snapshot Python core. See [container integration](agent-integration.md#可选-容器执行) and [Codex setup](codex.md).

Harbor and tau2 use separate dependency environments because their upstream dependencies conflict. The launcher installs the public `all` extra for regular experiments and adds `harbor` for Harbor benchmarks; it does not install system tools or provide benchmark-specific credentials and private corpora.

Keep train, dev, and test separate. If a benchmark only provides an evaluation pool, define an experimental split explicitly. A custom split for self-improvement is not automatically comparable to the official leaderboard.

## Read the results

Each run creates a directory under `run.output_dir`. The bundled quickstart writes to `runs/<algorithm>-<agent>-<benchmark>-<timestamp>/` and reports the path in the terminal.

| File | What to inspect |
| --- | --- |
| `end-summary.md` / `end-summary.json` | Run status, final test score, and recorded Target/Worker usage. |
| `result.json` | Selected `snapshot_id`, `test_score`, `test_metrics`, and `test_tasks`. |
| `snapshots/` | Initial and candidate resource versions, including the selected Snapshot. |
| `trajectories/` | Task outcomes, Worker calls, and candidate decisions. |
| `config.yaml` | Saved configuration with credentials redacted. |
| `error.json` | Runtime failure details, if an experiment directory was created. |
| `artifacts/harbor/trial-*/` | Harbor-collected deliverables, verifier details, logs, and `manifest.json`. |

For Harbor tasks, inspect the archived files before rerunning: SpreadsheetBench 2 workbooks are usually under `artifacts/logs/artifacts/`, with native scores in `verifier/native-result.json`. SWE-bench Pro records separate solve and regrade archives. These archives preserve collected files; they do not constitute an independent re-evaluation. See [artifact locations](experiment-launcher.zh-CN.md#查看结果).

Check that `test_tasks > 0` and inspect failed task trajectories. A completed experiment does not guarantee improvement or successful execution of every task. Recorded SDK usage may be incomplete and is not the provider's final bill.

The standard runner evaluates the selected Snapshot on test after search. It does not automatically evaluate the initial Snapshot on test. To measure improvement, compare both versions on the same held-out tasks and metric, as in the [SearchQA reproduction guide](searchqa-deepseek-reproduction.md).


## Evaluate a saved Snapshot on another benchmark

Use `flashrsi evaluate --config examples/evaluate_hotpotqa.yaml --snapshot /path/to/selected.json`.
Fill the Target model and credentials first. Add `--check-only` to validate setup without model calls.
This runs only the destination benchmark's test split, without a Worker or evolution.
The automatic launcher accepts the same `--snapshot` option. Agent type, root resource version and
component rules must match the saved Snapshot; model and connection settings come from the destination
configuration. See [Snapshot evaluation](snapshot-evaluation.md) for preparation, split selection and output records.
