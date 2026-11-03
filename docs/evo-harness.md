# Evo-Harness

**English** · [简体中文](./evo-harness.zh-CN.md)

[All algorithms](./integrations/algorithms.md) · [Example configuration](../examples/evo_harness.yaml)

Evo-Harness consumes training tasks once and compiles failures into native skills at two levels: topic-specific procedures and general cross-task guidance. Each batch uses one fixed Snapshot; compilation happens afterward. Valid changes are continuously adopted without a dev gate. The final Snapshot runs through ordinary Agent execution without an optimizer at inference.

## Requirements

The Target must expose `skill` and `config` components and natively discover/load directory skills. The algorithm edits only `skill`; `config` is read-only. Declaring components alone does not add a skill loader to a custom harness.

Train must be non-empty with unique task IDs. The Worker must be a file-editing Agent (`algorithm.worker.type: agent`); a text LLM Worker is rejected at startup. Configure Target and Worker models and connections independently.

There is no dev selection, repeated epoch, or retry of the current task. The runner evaluates the final frozen Snapshot on test. Test feedback is not used for reflection or compilation.

## Run a small example

Follow [Installation](./guide/installation.md). Configure both roles in [examples/quickstart.yaml](../examples/quickstart.yaml), then switch its Worker to an Agent and use its local QA data from the repository root:

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm evo_harness \
  --algorithm.worker.type agent \
  --algorithm.worker.name miniagent \
  --algorithm.worker.max_turns 10 \
  --algorithm.batch_size 3 \
  --algorithm.max_steps 2 \
  --algorithm.general_min_tasks 3
```

This consumes the six bundled train tasks in two batches, allowing observation of skill reuse by a later batch. It does not guarantee compilation: a batch needs usable failure memories, and a revised general skill needs at least three distinct supporting tasks here. Append `--check-only` for initialization without model calls; it does not verify model authentication or skill use.

The dedicated [Evo-Harness configuration](../examples/evo_harness.yaml) also uses MiniAgent for both roles. See [Configuration & CLI](./guide/configuration.md) and [harness integrations](./integrations/harnesses.md) when changing providers or harnesses.

## Search and selection

1. Consume train in its supplied order, limited by `max_steps` batches. Execute every task in a batch with the same current Snapshot and record scores before updates.
2. Reflect only on unfinished tasks or scores below `success_threshold`. The Worker receives recorded training evidence and returns `lesson`, `trigger`, `evidence`, and `scope_hint`, or `{"skip": true}`. Invalid memory JSON or required fields produce a decline record.
3. If usable memories exist, call the Topic Curator and then the General Curator through the same Worker's `edit()`. They create, merge, revise, or delete native skill files. The second level starts from the first level's accepted result.
4. Validate each level's committed changes against component permissions, ownership, provenance, level, and library budgets. Reject that entire level on invalid edits; a rejected general edit does not undo an already-accepted topic edit.
5. Adopt legal resource changes without comparing dev or online scores. The next batch uses the updated Snapshot; unchanged edits do not activate a new version.
6. Record `online_score` and `online_tasks`, then return current for final test.

`online_score` averages the one-time pre-update training outcomes. It measures a changing agent along the stream, not the final frozen agent's test performance. Keep it separate from `test_score`.

## What changes in a skill

Managed skills use native directories such as `skills/verify-evidence/SKILL.md`, with YAML frontmatter followed by a non-empty body that may contain code blocks:

```yaml
---
name: verify-evidence
description: Verify supplied evidence on evidence-backed questions
flashrsi:
  algorithm: evo_harness
  level: topic
  source_task_ids: [train-0]
---
```

`name` and `description` support native discovery; `flashrsi` records ownership, level, and source tasks. Each new or revised skill must cite usable memories from the current batch. A general skill needs `general_min_tasks` distinct source IDs. Old evidence does not automatically justify a new revision; unchanged skills retain their provenance in the library and Snapshot history.

Each Curator may edit only its requested level. Existing unmanaged skills cannot be taken over by adding provenance or changed as collateral edits. Removing provenance, omitting it from new files, or shadowing a flat native skill with a new directory skill is rejected. Checks inspect actual Snapshot changes, including frontmatter changes. They validate structure and source IDs, not the semantic truth or generality of a lesson.

MiniAgent retains its configured flat-skill loading and discovers directory `SKILL.md` files through the workspace loader, exposing `read_skill` for on-demand bodies. Claude Code uses native plugin discovery and its `Skill` tool; explicit tool allowlists must permit `Skill`. Curators do not need to modify configuration or `CLAUDE.md`. See [Agent integration](./agent-integration.md).

Worker execution failures propagate. Invalid memories can be skipped, and invalid compiled edits are declined. The algorithm does not execute candidate skill programs as part of validation; their behavior is exercised through later native Agent runs.

## Parameters and cost

These implementation defaults belong under `algorithm`.

| Field | Default | Meaning |
| --- | --- | --- |
| `batch_size` | `4` | Tasks sharing one Snapshot per batch; at least 1. |
| `max_steps` | `null` | Batch limit; positive integer or the entire train stream. |
| `success_threshold` | `1.0` | Reflect on unfinished tasks or scores below this value. |
| `general_min_tasks` | `3` | Distinct current-batch sources per new/revised general skill; at least 2. |
| `max_general_skills` | `10` | Managed general-skill count limit; at least 1. |
| `max_topic_skills` | `30` | Managed topic-skill count limit; at least 1. |
| `max_skill_chars` | `2000` | Character limit per managed skill body; at least 1. |
| `max_library_chars` | `48000` | Total normalized YAML serialization size of managed skill records, including content; at least 1. |
| `max_evidence_chars` | `24000` | Evidence character limit for reflection and compilation; at least 1. |

If `batch_size < general_min_tasks`, no new or revised general skill can meet the evidence requirement. Even a large enough batch may contain too few usable failure memories.

Each consumed train task runs once. Each failing result makes one reflection call, and a batch with at least one usable memory makes two directory-edit calls. Native Agent turns within those calls and final test are additional costs. Library budgets constrain compiled artifacts, not the number or size of skills loaded into a task's context.

## Inspect an experiment

`declined` records distinguish `memory` parsing failures from `compile` validation failures. A `compile` decision includes `level`, `step`, parent/candidate IDs, `changed_paths`, and `accepted`. Acceptance means legal changes were adopted, not that performance improved. An unchanged compile has `accepted: false`.

The final `stream` decision identifies the retained Snapshot and online metric. To assess reuse, inspect a later batch or frozen test for native skill discovery and actual loading; displaying a description alone does not prove the body was used.

## Source and adaptation

The [implementation](../flashrsi/algorithms/evo_harness/__init__.py) pins `A-EVO-Lab/a-evolve`, branch `release/evo-harness`, to `3c7c7b8c62a3f07da1d4407aa03f0ca079954bcf`, with paper identifier `2608.15071v2`. [Prompts](../flashrsi/algorithms/evo_harness/_prompts.py) define reflection and compilation roles.

The port reflects from recorded context rather than continuing the solver session. Native skill selection/loading replaces a separate Selector call; Curators edit native files instead of a JSON operation library embedded into a prompt. Final Snapshots are independently runnable. Historical prompt-embedded JSON libraries do not satisfy this file protocol and require re-evolution. Current-revision evidence comes from the current batch rather than inherited source counts. These changes preserve the workflow but do not reproduce the complete upstream experiment.
