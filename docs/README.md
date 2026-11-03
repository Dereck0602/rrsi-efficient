# FlashRSI documentation

[Project page](/project.html) · [Website quickstart](index.md) · [All integration notes](reference.md) · [Preview and GitHub Pages hosting](development/docs-site.md)

To browse these pages as a documentation website, run `npm ci` and `npm run docs:dev` from the repository root (Node.js 22+), then open the printed URL.

[Back to the project README](../README.md) · [简体中文 usage guide](usage.zh-CN.md) · [English usage guide](usage.md)

FlashRSI lets you apply a resource-level self-improvement method to an existing agent, run the resulting
candidate through its native harness, and measure it on a benchmark. Start with the guide if you want to
run an experiment; use the integration and development sections when you want to add a method, benchmark,
or harness.

## Guide

### Install

Use Python 3.11 or newer. Python 3.12 is recommended for the full benchmark matrix.

```bash
git clone https://github.com/RUC-NLPIR/FlashRSI.git
cd FlashRSI
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[all]"
```

Harbor is the only separate optional extra:

```bash
python -m pip install -e ".[all,harbor]"
```

Docker is required by Harbor and several task environments. Install [Docker Desktop or Docker Engine](https://docs.docker.com/get-docker/), start the daemon, and check it with:

```bash
docker run --rm hello-world
```

The [benchmark configuration index](benchmark-quickstart.md) lists the service, Docker, data, and
Python-version requirements for each benchmark.

### Run an experiment

The bundled quickstart runs SkillOpt with MiniAgent on a 12-question SearchQA sample. Target and Worker
models use independent settings:

```bash
export TARGET_API_KEY="..."
export WORKER_API_KEY="..."

flashrsi run \
  --config examples/quickstart.yaml \
  --algorithm skillopt \
  --benchmark searchqa \
  --agent-harness miniagent \
  --model gpt-5-mini \
  --worker-model gpt-5-mini \
  --set agent.llm.api_key="$TARGET_API_KEY" \
  --set algorithm.worker.llm.api_key="$WORKER_API_KEY"
```

For a longer run, start from a method-specific configuration in [`examples/`](../examples/). Use the
[experiment guide](running-experiments.md) to choose an algorithm, harness, benchmark, Worker, and split.

Useful checks:

```bash
flashrsi run --config examples/quickstart.yaml --check-only
flashrsi run --config examples/quickstart.yaml --prepare-only
```

`--check-only` prepares dependencies and data without model calls or a result directory. `--prepare-only`
only writes the redacted configuration directory.

### Evaluate a Snapshot

A Snapshot is a saved, executable version of an agent's declared resources. Evaluate the selected or an
intermediate candidate without continuing evolution:

```bash
flashrsi evaluate \
  --config examples/evaluate_hotpotqa.yaml \
  --snapshot /path/to/run-A/snapshots/SELECTED_ID.json \
  --check-only

flashrsi evaluate \
  --config examples/evaluate_hotpotqa.yaml \
  --snapshot /path/to/run-A/snapshots/SELECTED_ID.json
```

The current CLI uses the subcommand `evaluate` for the operation often called `eval`. It runs only the
destination benchmark's test split; it does not create a Worker or continue search. See [Snapshot
evaluation](snapshot-evaluation.md) for compatibility rules, split selection, and output files.

### Configure an experiment

Configuration is YAML with three independent roles:

| Role | Configuration | Responsibility |
| --- | --- | --- |
| Target | `agent` | Executes benchmark tasks with the selected Snapshot. |
| Worker | `algorithm.worker` | Proposes edits or edits candidate files. |
| Benchmark | `benchmark` | Provides tasks, environments, and grading. |

The most common fields are:

| Area | Fields | Why it matters |
| --- | --- | --- |
| Run | `run.output_dir`, `run.mode`, `run.snapshot` | Select output location or Snapshot evaluation mode. |
| Target | `agent.name`, `agent.llm.*`, `agent.max_turns` | Select a harness and its model/runtime settings. |
| Algorithm | `algorithm.name`, algorithm-specific fields | Select the search policy and budget. |
| Worker | `algorithm.worker.type`, `algorithm.worker.llm.*` | Choose an LLM Worker or Agent Worker. |
| Benchmark | `benchmark.name`, `split`, `split_counts`, `data_path` | Select data, task counts, and source paths. |

Every nested field can be overridden from the command line with a dotted option or `--set PATH=VALUE`.
Keep API keys in environment variables or command-line expansion; they are redacted from saved run records.
See [Usage guide](usage.md) for the complete configuration examples and Python API.

## Integrations

### Algorithms

FlashRSI currently registers thirteen method adaptations. The shared runner supplies Snapshot checkout,
rollout, scoring, and records; each algorithm keeps its own proposal, feedback, selection, and adoption
policy.

| Method | Scope | Main control | Detailed guide |
| --- | --- | --- | --- |
| OPRO | Instructions | Scored history | [Guide](opro.md) · [中文](opro.zh-CN.md) · [Configuration](../examples/opro.yaml) |
| GEPA | Instructions | Candidate pool and reflection | [Guide](gepa.md) · [中文](gepa.zh-CN.md) · [Configuration](../examples/gepa.yaml) |
| EvoPrompt | Instructions | GA / DE population | [Guide](evoprompt.md) · [中文](evoprompt.zh-CN.md) · [Configuration](../examples/evoprompt.yaml) |
| ACE | Instructions / playbook | Playbook accumulation | [ACE](ace.md) · [中文](ace.zh-CN.md) |
| SkillOpt | Skills | Fast / slow updates | [SkillOpt guide](skillopt.md) · [Quickstart](../examples/quickstart.yaml) |
| Evo-Harness | Skills | Task-stream accumulation | [Evo-Harness](evo-harness.md) · [中文](evo-harness.zh-CN.md) |
| AHE | Harness resources | Successive edits and audit | [AHE](ahe.md) · [中文](ahe.zh-CN.md) |
| ADAS | Harness resources | Design archive | [ADAS](adas.md) · [中文](adas.zh-CN.md) |
| DarwinX | Harness resources | Branches and recombination | [DarwinX](darwinx.md) · [中文](darwinx.zh-CN.md) |
| Meta-Harness | Harness resources | File-based history | [Meta-Harness](meta-harness.md) · [中文](meta-harness.zh-CN.md) |
| ModularRSI | Modules and harness resources | Branch integration | [ModularRSI](modular-rsi.md) · [中文](modular-rsi.zh-CN.md) |
| RRSI | Harness resources | Reliability and cost admission | [RRSI](rrsi.md) |
| Arbor | Harness resources | Hypothesis tree and propagated insights | [Arbor](arbor.md) |

Implementations are adaptations to FlashRSI's resource and evaluation interfaces. A registered method is
available through the runner, but registration does not mean every method, harness, and benchmark
combination has a completed model run or reproduces an upstream paper exactly.

### Benchmarks and datasets

Benchmark adapters own task loading and grading. They may read pinned Hugging Face data, a local
`data_path`, a native task package, or a simulator. The Agent Snapshot remains separate from the benchmark
environment: task files, Docker images, databases, service credentials, and verifiers are supplied by the
benchmark integration.

| Domain | Covered tasks | Details |
| --- | --- | --- |
| Question answering | SearchQA, HotpotQA, MuSiQue, TriviaQA, Natural Questions, 2WikiMultiHopQA | [QA setup notes](benchmark-quickstart.md) |
| Reasoning and mathematics | BBEH, SuperGPQA, HLE, MATH, AIME, LiveMathBench | [Lightweight benchmarks](lightweight-benchmarks.md) · [Math](math-independent-evolution.md) |
| Coding | SWE-bench Verified, SWE-bench Pro, DeepSWE, Terminal-Bench | [Coding setups](coding-working-benchmarks.md) · [Terminal-Bench](terminal-bench.md) |
| Web research | BrowseComp, DeepSearchQA, WideSearch | [Web services](web-research-services.md) |
| Documents and tool use | OfficeQA-Pro, Harvey LAB, JobBench, SpreadsheetBench 2, AppWorld, BFCL, WorkBench, TravelPlanner | [Configuration index](benchmark-quickstart.md) |
| Interactive environments | τ²-bench, τ³ Banking, ALFWorld | [τ²](tau2-bench.md) · [ALFWorld](alfworld.md) |

Use `benchmark.split` or `split_counts` to define an experimental train/dev/test size. Native test-only
collections keep their source test role; a custom split should be reported as an experiment split rather
than as an official leaderboard evaluation. The [dataset cards](../scripts/dataset_card.md) record source,
revision, license, and preparation details for released bundles.

### Harnesses

A harness adapter declares the resources that may be versioned and calls the harness's native entry point.
The same adapter can serve as a Target that solves tasks or an Agent Worker that edits a candidate.

| Harness | Editable resources | Integration status | Details |
| --- | --- | --- | --- |
| MiniAgent | Prompts, skills, tools, configuration, runtime code | Available | [Usage](usage.md#native-harnesses) |
| Claude Code | Instructions, skills, agents, hooks, MCP/LSP, settings | Available | [Agent integration](agent-integration.md) |
| Codex | Python core, instructions, skills, plugins, memory, rules, configuration | Available | [Codex guide](codex.md) |
| Pi | Pure-Python runtime, instructions, skills, prompt templates, settings | Available | [Integration contract](agent-integration.md) |
| Hermes | Python runtime, SOUL instructions, skills, tool settings | Available | [Integration contract](agent-integration.md) |
| OpenClaw | Pure-Python Snapshot runtime, bootstrap files, skills, tools, policy, session and memory settings | Available | [Integration contract](agent-integration.md) |
| OpenCode | Pure-Python Snapshot runtime, instructions, coding tools, permissions and session settings | Available | [Integration contract](agent-integration.md) |

For task environments and Harbor boundaries, see [Harness and benchmark responsibilities](benchmark-harness-boundary.md).

## Developments

### Framework and Snapshot

FlashRSI maps each harness into a tagged resource tree. A Snapshot stores the complete declared resource
version used for execution; an editing scope controls what an algorithm can read or change. The surrounding
resources remain in the executable candidate, so a skill or prompt is evaluated together with the tools and
runtime that give it meaning.

```text
Tagged Snapshot files
        │
        ├── scoped view ──► algorithm edits and commits a candidate
        │
        └── complete candidate ──► native harness ──► benchmark trajectory and score
```

Credentials, task data, temporary logs, and runtime state stay outside the Snapshot. Candidate lineage,
changed paths, trajectories, scores, and usage are stored in the run directory. Read [How it works](usage.md#how-it-works)
and [Snapshot evaluation](snapshot-evaluation.md) before changing the resource boundary.

### Add a benchmark

There are two common paths:

1. **Harbor or another environment runtime.** Use this when tasks need Docker, a verifier, a database, a
   simulator, or isolated dependencies. Keep the task package and environment setup in the benchmark
   integration; bind the environment runner to the Agent and return a normal `Trajectory`.
2. **Raw files or a local/native runner.** Use this for text, JSONL, documents, or a local simulator. Load
   tasks into `Task` objects, keep the source and revision in metadata, and implement a deterministic grader.

Register the benchmark, add a YAML example, document its metric and split policy, and add an offline
contract test. Start with the [benchmark interface](../flashrsi/core/benchmark.py), the [configuration index](benchmark-quickstart.md),
and [Harbor integration notes](terminal-bench.md).

### Add a harness

Choose the smallest mapping that preserves the harness behavior:

- **Closed or interface-first:** map stable CLI, SDK, or configuration interfaces into tagged files. Keep
  proprietary internals in the native product.
- **Clean open source:** map the source tree directly, tag each file by role, and execute the selected
  Snapshot through the native entry point. MiniAgent follows this pattern.
- **Complex open source:** reduce or port the execution surface against a pinned upstream baseline,
  document supported behavior and migration limits, then align that implementation with the Snapshot.
  Codex uses a Python semantic port of supported paths from a fixed Rust baseline.

Add `components.yaml`, implement an `Agent` subclass and `_run`, return a `Trajectory`, and keep credentials
and task state outside versioned resources. The [Agent integration guide](agent-integration.md) contains the
adapter contract and the optional container path.

### Add an algorithm

Subclass `Algorithm`, declare the observation and editing scope, and keep proposal, reflection, selection,
and memory inside `evolve`. Use the shared Snapshot checkout, target rollout, benchmark grading, and record
facilities. The runner should assemble collaborators; it should not decide the method's search policy.

Add a configuration example, a focused offline test for the algorithm's state transitions, and a short guide
that states its scope, feedback, adoption rule, budget, and known differences from the upstream method. The
[usage guide's extension section](usage.md#extend-flashrsi) and the existing [algorithm modules](../flashrsi/algorithms)
show the expected boundaries.

## FAQ

- **What is the difference between a Snapshot and a run?** A Snapshot is an executable resource version; a
  run is the record of an evolution or evaluation process that may contain many Snapshots.
- **Can I reuse a Snapshot on another benchmark?** Yes, with a compatible Agent resource root and component
  map. Use `flashrsi evaluate` and follow the [cross-benchmark evaluation rules](snapshot-evaluation.md).
- **Does a registered benchmark imply a completed result?** No. Check the benchmark guide and run records for
  the supported track, environment, and validation status.
- **Where should I start?** Run the bundled quickstart, then read [Choose and run an experiment](running-experiments.md).

## Reference map

| Need | Start here |
| --- | --- |
| First run | [Quickstart](#run-an-experiment) |
| CLI and YAML settings | [Usage guide](usage.md) |
| Algorithm and benchmark choice | [Experiment guide](running-experiments.md) |
| Snapshot transfer evaluation | [Snapshot evaluation](snapshot-evaluation.md) |
| Harness adapter contract | [Agent integration](agent-integration.md) |
| Benchmark environments | [Benchmark index](benchmark-quickstart.md) |
| Development history | [Implementation notes](implementation-notes.zh-CN.md) |
