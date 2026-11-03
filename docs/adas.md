# ADAS

**English** · [简体中文](./adas.zh-CN.md)

[All algorithms](./integrations/algorithms.md) · [Example configuration](../examples/adas.yaml)

ADAS searches agent designs using an archive of design descriptions and measured scores. Every new proposal starts from the current best-dev Snapshot and is revised before evaluation. Low-scoring designs remain in the archive, but only a strict dev improvement changes the next parent and eventual winner. The Target's active version is promoted once, after search.

## Requirements

Dev must be non-empty; train is unused. Dev scores feed subsequent design proposals and selection, so dev is a search split, not an independent final test. The runner evaluates the selected Snapshot on test afterward.

The Worker must be a file-editing Agent (`algorithm.worker.type: agent`) with independent model and connection settings. The design is the Target's declared resource tree: `evolve_scope = {"*"}` allows all declared components. Runtime-code editing is available only when the Target exposes it; this does not expose closed harness internals. See [harness integrations](./integrations/harnesses.md).

## Run a small example

Follow [Installation](./guide/installation.md). Configure the Target and Worker in [examples/quickstart.yaml](../examples/quickstart.yaml), then use its local data from the repository root:

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm adas \
  --algorithm.worker.type agent \
  --algorithm.worker.name miniagent \
  --algorithm.worker.max_turns 30 \
  --algorithm.max_steps 1 \
  --algorithm.num_reflections 1
```

This requests one design and one pre-evaluation review. Set `num_reflections: 0` to omit review; that run would not cover the reflection phase. Append `--check-only` for initialization without model calls or remote authentication validation. The dedicated [ADAS configuration](../examples/adas.yaml) uses two review passes. See [Configuration & CLI](./guide/configuration.md) for overrides.

## Search and selection

1. Evaluate the initial Snapshot on full dev. Seed the archive with its description and score, and make it the current best.
2. Check out the best Snapshot. Give the Worker the scored archive and Target component rules, then request a new design through file edits and a `<DESIGN>` description.
3. Run `num_reflections` further editing calls in that same checkout. Each sees the latest description and component rules; only the proposal prompt explicitly includes the archive. These reviews happen before measurement, with no intermediate framework evaluation.
4. Remove known runtime artifacts and commit once. Empty or invalid resource changes are declined without candidate evaluation, consuming one step.
5. Evaluate a valid candidate on full dev. Append its final description and score to the archive whether it improved or regressed. Strictly higher dev changes `best`; a tie keeps the earlier selected version.
6. Repeat from best, not from the last candidate when it lost. At the end, promote best and record the archive and declined designs.

The archive passed to the Worker contains `account` and `score`, not full historical source trees or execution traces. The complete list is retained, but its prompt serialization is truncated by `max_evidence_chars`. Unlike [Meta-Harness](./meta-harness.md), this implementation does not provide a filesystem history for selective retrieval.

## Design descriptions and invalid edits

The last `<DESIGN>...</DESIGN>` block becomes the design account. If no complete tag pair exists, the entire reply is used. There is no AHE-style per-file prediction manifest; the edited files define the candidate and the description explains it to later proposals.

Declined edits have no score, so they do not enter the scored archive. Their reasons are recorded in decisions and final `declined_designs`, but are not fed into the next proposal prompt. Low-scoring *evaluated* designs do enter the archive.

Worker failures propagate. Candidate runtime errors are handled by the Target and Benchmark contracts, not an ADAS-specific repair loop. In particular, malformed native output or a broken execution interface can raise an exception and abort search; it is not guaranteed that every broken design becomes a zero-scored archive entry.

## Parameters and cost

Implementation defaults belong under `algorithm`.

| Field | Default | Meaning |
| --- | --- | --- |
| `max_steps` | `10` | Design attempts; integer, at least 1. Invalid attempts count. |
| `num_reflections` | `2` | Additional editing reviews in the same checkout before evaluation; integer, at least 0. |
| `max_evidence_chars` | `30000` | Character limit for each serialized evidence block, including archive and rules separately; at least 1. |

For D dev tasks and S attempts, Target search costs at most `(1 + S) * D` task executions, including the seed. Each normally completed attempt makes `1 + num_reflections` Worker editing calls. Invalid edits skip candidate dev evaluation but still spend the editing calls. Native model/tool turns, backend retries, and final test are additional.

## Inspect an experiment

`kind: admit` means a measured design entered the archive. Its `accepted` field specifically indicates whether it became the new best/next parent. Inspect `parent_id`, `candidate_id`, `parent_score`, `candidate_score`, and `account` to distinguish exploration from selection.

`kind: decline` explains unmeasured attempts. The final `promote` contains the selected `candidate_id`, full `archive`, and `declined_designs`. Archive rows themselves contain descriptions and scores; the per-attempt decisions provide the Snapshot linkage.

## Source and adaptation

The [search implementation](../flashrsi/algorithms/adas/__init__.py) and [prompts](../flashrsi/algorithms/adas/_prompts.py) reference `ShengranHu/ADAS`, but **the upstream commit remains unpinned** in this repository. This is a mechanism port with a provenance gap to resolve, not a verified revision-level reproduction.

The original fixed-framework `forward()` design is represented here by native harness files. The archive starts from one Target Snapshot rather than several predefined agent architectures. Reflection edits the same checkout, descriptions travel in replies, component rules constrain the design, and final selection uses FlashRSI's dev score. No separate agent DSL, multi-seed architecture library, or cross-process search resume is implemented.
