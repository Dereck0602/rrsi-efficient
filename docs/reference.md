# All integration notes

Use these detailed notes for version boundaries, method settings, source revisions, and environment-specific behavior. Some existing notes are in Chinese; the main guides introduce each topic in English.

## Usage and experiments

- [English usage and Python API](./usage.md) · [简体中文使用指南](./usage.zh-CN.md)
- [Choose and run an experiment](./running-experiments.md)
- [Automatic experiment launcher](./experiment-launcher.zh-CN.md)
- [Snapshot evaluation reference](./snapshot-evaluation.md)
- [Snapshot storage](./guide/snapshot-storage.md)
- [SearchQA × DeepSeek reproduction](./searchqa-deepseek-reproduction.md)
- [Documentation overview](./README.md)

## Method implementations

| Method | Guide |
| --- | --- |
| OPRO | [Scored history and train selection](./opro.md) · [中文](./opro.zh-CN.md) |
| GEPA | [Per-task frontier, screening, and budgets](./gepa.md) · [中文](./gepa.zh-CN.md) |
| EvoPrompt | [GA / DE populations and selection](./evoprompt.md) · [中文](./evoprompt.zh-CN.md) |
| SkillOpt | [Fast / slow updates, selection, and budgets](./skillopt.md) |
| ACE | [Playbook, modes, and parameters](./ace.md) · [中文](./ace.zh-CN.md) |
| Evo-Harness | [Native skill loading and task streams](./evo-harness.md) · [中文](./evo-harness.zh-CN.md) |
| AHE | [Edits, audits, and manifest](./ahe.md) · [中文](./ahe.zh-CN.md) |
| ADAS | [Design archive and reflections](./adas.md) · [中文](./adas.zh-CN.md) |
| DarwinX | [Branches, recombination, and admission](./darwinx.md) |
| Meta-Harness | [File history and candidate search](./meta-harness.md) · [中文](./meta-harness.zh-CN.md) |
| ModularRSI | [Modules, integration, and evolution data](./modular-rsi.md) |
| RRSI | [Selection rules, budgets, and costs](./rrsi.md) |
| Arbor | [Hypothesis tree, trunk adoption, and insight propagation](./arbor.md) |

## Benchmarks and environments

- [All 30 benchmark configurations](./benchmark-quickstart.md)
- [Harness / benchmark boundary](./benchmark-harness-boundary.md)
- [BBEH and SuperGPQA](./lightweight-benchmarks.md)
- [AIME](./coding-math-benchmarks.md)
- [Coding and SpreadsheetBench 2](./coding-working-benchmarks.md)
- [Terminal-Bench](./terminal-bench.md) · [DeepSWE](./deepswe.md)
- [Web research services](./web-research-services.md)
- [τ²-bench](./tau2-bench.md) · [ALFWorld](./alfworld.md) · [WorkBench](./workbench.md)

## Dataset and evolution sources

- [SearchQA](./searchqa-independent-evolution.md)
- [HotpotQA](./hotpot-independent-evolution.md)
- [MuSiQue](./musique-independent-evolution.md)
- [TriviaQA](./triviaqa-independent-evolution.md)
- [2WikiMultiHopQA](./twowiki-independent-evolution.md)
- [BBEH](./bbeh-independent-evolution.md)
- [SuperGPQA](./supergpqa-independent-evolution.md)
- [MATH-500](./math-independent-evolution.md)
- [SWE-bench](./swe-independent-evolution.md)
- [AppWorld](./appworld-independent-evolution.md)

## Harness implementation details

- [Agent adapter and optional container execution](./agent-integration.md)
- [Codex setup and capability limits](./codex.md)
- [Codex pinned baseline and Python port](./codex-core-extraction.md)
- [Codex task mechanisms](./codex-task-mechanisms.md)
- [Codex migration and evolution plan](./codex-evolution-plan.md)

## Development history

These are historical implementation notes and plans. Use the main guides and module documentation for the current interface.

- [Early implementation records](./implementation-notes.zh-CN.md)
- [Benchmark and algorithm migration plan](./migration-plan.md)
- [MVP interface drift review](./mvp-doc-drift.md)
