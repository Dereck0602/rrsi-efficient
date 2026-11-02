---
title: Quickstart
description: Run a first reproducible FlashRSI experiment and keep the evidence behind the score.
prev: false
next:
  text: Installation
  link: /guide/installation
---

<div class="quickstart-intro">
  <p class="intro-context">FlashRSI · Field guide</p>
  <h1>Run a first self-improvement experiment.</h1>
  <p class="intro-lead">Give an RSI method a defined editing scope. Run each candidate through a native harness and keep the evidence behind the score.</p>
  <div class="intro-actions">
    <a href="#run-the-experiment">Start with the run →</a>
    <a href="./development/framework.html">Understand the design</a>
    <a href="./project.html">Open the project page</a>
  </div>
</div>

This quickstart runs **SkillOpt with MiniAgent** on 12 bundled SearchQA questions. It uses six questions for train, three for dev, and three for test. You need Python and two model configurations; Docker is not required.

## What FlashRSI is

FlashRSI gives research teams one experiment interface for changing agent resources, running the complete candidate, and comparing the result across tasks. The method, harness, worker, and benchmark stay explicit in one configuration.

<div class="guide-pillars">
  <div class="guide-pillar"><strong>Defined scope</strong><p>Algorithms declare which prompts, skills, tools, and runtime files they may edit.</p></div>
  <div class="guide-pillar"><strong>Native execution</strong><p>The full candidate runs through MiniAgent, Claude Code, Codex, Pi, Hermes, OpenClaw, or OpenCode.</p></div>
  <div class="guide-pillar"><strong>Shared evidence</strong><p>Snapshots, trajectories, usage, and scores stay under one run directory.</p></div>
  <div class="guide-pillar"><strong>Broad coverage</strong><p>13 methods and 30 benchmark suites use the same runner.</p></div>
</div>

## Install FlashRSI

From a terminal:

```bash
git clone https://github.com/RUC-NLPIR/FlashRSI.git
cd FlashRSI
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[all]"
```

Python 3.11+ is supported. Python 3.12 covers the widest set of benchmark integrations. See [Installation](./guide/installation.md) for Harbor, τ², Windows activation, and Docker.

## Run the experiment {#run-the-experiment}

The **Target** answers benchmark questions. The **Worker** proposes changes to its skills. Set their credentials separately before running from the repository root:

```bash
export TARGET_API_KEY="your-target-key"
export WORKER_API_KEY="your-worker-key"

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

Use model names supported by your provider. For a custom endpoint, set `agent.llm.base_url` and `algorithm.worker.llm.base_url`. The complete parser reference is in [Configuration & CLI](./guide/configuration.md).

::: tip Check setup first
Run `flashrsi run --config examples/quickstart.yaml --check-only` to check startup without model calls. It may prepare data and dependencies; it does not test remote model authentication.
:::

## Choose the parts of a run

Change one part while keeping the others visible. That is what makes a comparison useful.

<div class="run-slots">
  <div class="run-slot"><small>Algorithm</small><strong>How to search</strong><p>OPRO, GEPA, ACE, SkillOpt, RRSI, and other methods propose and select edits.</p></div>
  <div class="run-slot"><small>Harness</small><strong>Where to execute</strong><p>A native harness provides the prompts, tools, runtime, and execution boundary.</p></div>
  <div class="run-slot"><small>Benchmark</small><strong>What to measure</strong><p>Tasks, data, grading rules, and splits turn a candidate into a comparable result.</p></div>
</div>

See the supported [algorithms](./integrations/algorithms.md), [benchmarks](./integrations/benchmarks.md), and [harnesses](./integrations/harnesses.md) before choosing a combination.

## How a run works

Every experiment follows the same evidence path. The method can change; the boundary stays legible.

<ol class="run-lifecycle">
  <li><strong>Capture the baseline</strong><p>Build the initial AgentSnapshot from the harness components and record its identity.</p></li>
  <li><strong>Propose a candidate</strong><p>The algorithm uses Target feedback or Worker edits inside its declared resource scope.</p></li>
  <li><strong>Run the complete candidate</strong><p>The native harness executes the candidate and records the trajectory.</p></li>
  <li><strong>Score and select</strong><p>The benchmark grades the trajectory; the algorithm keeps or rejects the Snapshot.</p></li>
  <li><strong>Inspect the evidence</strong><p>Read the config, resource lineage, calls, artifacts, and final test result together.</p></li>
</ol>

## Inspect the output

The command prints the experiment directory. The sample configuration writes it under `runs/`, relative to the repository root.

| File | What to look for |
| --- | --- |
| `end-summary.md` | Completion status, final test score, and recorded usage. |
| `result.json` | The selected `snapshot_id`, `test_score`, and `test_tasks`. |
| `snapshots/` | Initial and candidate resource versions. |
| `trajectories/` | Task answers, Worker calls, and selection decisions. |
| `config.yaml` | The resolved configuration with credentials redacted. |

Check that the tasks ran, then inspect failed trajectories. This small sample checks the setup; its score is not a research result. To measure an improvement, evaluate the initial and selected Snapshots on the same held-out tasks.

## Evaluate a saved Snapshot

Use any saved compatible candidate, including an intermediate Snapshot:

```bash
flashrsi evaluate \
  --config examples/evaluate_hotpotqa.yaml \
  --snapshot /path/to/run/snapshots/SELECTED_ID.json \
  --model gpt-5-mini \
  --set agent.llm.api_key="$TARGET_API_KEY"
```

This evaluates the destination benchmark's test split without starting a Worker or continuing the search. Read [Snapshot evaluation](./guide/evaluation.md) before transferring resources between experiments.

## From here

<div class="guide-links">
  <a class="guide-link" href="./running-experiments.html"><small>Run experiments</small><strong>Choose a full run</strong><span>Compare methods, harnesses, splits, and output settings.</span></a>
  <a class="guide-link" href="./development/framework.html"><small>Build on FlashRSI</small><strong>Understand the Snapshot</strong><span>See how tagged resources create a portable candidate.</span></a>
  <a class="guide-link" href="./project.html"><small>Project page</small><strong>Browse the results</strong><span>Open the benchmark surface and the evidence model.</span></a>
</div>
