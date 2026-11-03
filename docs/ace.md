# ACE

**English** · [简体中文](./ace.zh-CN.md)

[All algorithms](./integrations/algorithms.md) · [Example configuration](../examples/ace.yaml)

ACE grows a playbook through a Generator → Reflector → Curator loop. Valid additions and usage-counter changes are adopted immediately, allowing learning to continue through score regressions. Offline mode periodically measures dev and returns the best measured version; online mode retains the final accumulated playbook.

## Requirements

The Target must declare an `instruction_file` in its `prompt` component. The Worker can be a text LLM or an Agent: both reflection and curation use `ask()` replies, not directory editing.

Train must be non-empty; offline also requires dev. Environment tasks with `Task.env` set require `max_reflection_rounds: 1`. Multi-round retries prepend a diagnosis to the question; there is no generic environment-injection protocol, so that combination is rejected at startup.

Both modes provide training `ground_truth` to the Reflector when available; online is not an unlabeled online reproduction. Dev/test trajectories do not reach the Reflector or Curator. The runner owns final test evaluation.

## Run a small example

Follow [Installation](./guide/installation.md). Independently configure both roles in [examples/quickstart.yaml](../examples/quickstart.yaml), then use its local QA data from the repository root:

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm ace \
  --algorithm.mode offline \
  --algorithm.num_epochs 1 \
  --algorithm.max_steps 2 \
  --algorithm.eval_steps 1 \
  --algorithm.max_reflection_rounds 1
```

For ordered online consumption:

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm ace \
  --algorithm.mode online \
  --algorithm.num_epochs 1 \
  --algorithm.max_steps 2 \
  --algorithm.max_reflection_rounds 1
```

These commands inherit `batch_size: 2` and `edit_budget: 1`. Append `--check-only` for initialization without model calls or remote authentication checks. The dedicated [ACE configuration](../examples/ace.yaml) enables two reflection rounds. See [Configuration & CLI](./guide/configuration.md) for credentials and overrides.

## Search and selection

1. Load the managed playbook suffix. Offline evaluates the initial Snapshot on dev.
2. Execute a training batch with current. Online records its initial-attempt scores before reflection, retries, or updates.
3. Reflect on outputs, scores, status, errors, and reference answers, including correct attempts. If any task is unfinished or below `success_threshold`, retry while reflection rounds remain. Each retry uses the original question plus only the latest diagnosis, with the same Snapshot. The Curator receives the last diagnosis and its corresponding attempt.
4. Apply helpful/harmful bullet tags to counters and immediately adopt any changed tally. Neutral and unknown tags do not change counters.
5. Every `curator_frequency` batches, request ADD operations from that batch's last diagnosis and evidence. Adopt valid changes without a score gate. Invalid operations and size/budget violations are declined; the last four declines inform later curation.
6. Offline evaluates dev every `eval_steps` batches, even when curation was skipped or declined. Only a strictly higher score replaces the stored best. At the end, evaluate current if it differs from the last validated version, then restore the best measured Snapshot.
7. Online skips dev and restoration, returning current. The runner evaluates the selected Snapshot on test.

### Offline and online

| Behavior | Offline | Online |
| --- | --- | --- |
| Order | Shuffle with `seed + epoch` | Preserve order within each pass |
| Passes | `num_epochs` | Current implementation also uses `num_epochs`; set 1 for a single pass |
| Dev | Initial, scheduled, and final when needed | Not accessed; can be empty |
| Final version | Best measured dev Snapshot | Last accumulated Snapshot |
| Summary | `promote` with dev `score` | `stream` with `online_score` and `online_tasks` |

`online_score` averages each consumed task occurrence's initial attempt before its batch updates the playbook. Retries are excluded. With `num_epochs > 1`, tasks are consumed and counted again; use `num_epochs: 1` for single-pass reporting. This stream metric and final `test_score` measure different behavior and must be reported separately.

## What changes in the playbook

ACE preserves the instruction prefix and manages only the suffix after `<!-- flashrsi:ace-playbook -->`. Existing suffixes are reused; ordinary lists in the original instructions are not converted into managed bullets. No-op updates create no resource change.

Curation is ADD-only. The implementation assigns sectioned IDs and initial counters, collapses multiline content into one bullet, and deduplicates after whitespace normalization and case folding. The edit limit is checked before deduplication. Unrecognized new sections fall back to `others`, except `general`. Models cannot assign authoritative bullet IDs.

MiniAgent loads the playbook through its native instruction file; Claude Code appends it through `CLAUDE.md` while retaining its native system prompt. The algorithm does not branch on harness names. See [Agent integration](./agent-integration.md).

Recognized Worker turn, time, or token exhaustion is recorded as `worker_budget_exhausted`. A missing reflection supplies neither tags nor Curator input. A failed curation preserves current, including any already-adopted tally. Other Worker failures propagate. Semantic deduplication, playbook compression, and evolution checkpoint-resume are not implemented.

## Parameters and cost

These implementation defaults belong under `algorithm`.

| Field | Default | Meaning |
| --- | --- | --- |
| `mode` | `offline` | `offline` or `online`; determines validation and selection. |
| `num_epochs` | `1` | Train passes in either mode; integer, at least 1. |
| `batch_size` | `1` | Training tasks per batch; at least 1. |
| `max_steps` | `null` | Total batch limit; positive integer or `null`. |
| `edit_budget` | `4` | ADD limit per curation before deduplication; at least 1. |
| `eval_steps` | `100` | Offline dev interval in training batches; at least 1. |
| `max_reflection_rounds` | `3` | Diagnoses per batch, with at most one fewer retries; at least 1. |
| `curator_frequency` | `1` | Curate every this many batches; at least 1. |
| `playbook_token_budget` | `80000` | Approximate suffix limit: `ceil(characters / 4)`, excluding the original prefix; at least 1. |
| `success_threshold` | `1.0` | All tasks must finish and score at least this value to stop retries. |
| `max_evidence_chars` | `24000` | Serialized training evidence character limit; at least 1. |
| `seed` | `42` | Integer offline shuffle seed; online does not shuffle. |

A batch can execute its tasks up to `max_reflection_rounds` times and make that many reflection calls, plus a scheduled Curator call. Offline adds initial, periodic, and possibly final dev passes. `max_steps` is not a model-call budget; `playbook_token_budget` is not a tokenizer measurement.

## Inspect an experiment

`tally` and `curate` decisions with `accepted: true` mean immediate adoption, not dev improvement. `declined` identifies invalid curation or recognized Worker budget exhaustion. Offline ends with `promote`; online ends with `stream`. Read Snapshot IDs to distinguish the last learned version from the selected best, and report final test separately.

## Source and adaptation

The [implementation](../flashrsi/algorithms/ace/__init__.py) and [playbook protocol](../flashrsi/algorithms/ace/_playbook.py) name FlashRSI commit `5efe9b438483710060183c6c3ecbc5062b410a58` as the port reference and `ace-agent/ace` commit `82709de050e1db6e6ef2f07bcb0393560b94992a` as upstream. The original migration record did not independently verify that upstream revision; this guide describes local behavior.

Local choices include an instruction suffix instead of a skill, explicit training answers in evidence, retries only between diagnoses, dev scheduling independent of successful ADDs, and restoration through the existing Snapshot. No separate Trainer/Recipe/checkpoint layer is introduced. [Prompts](../flashrsi/algorithms/ace/_prompts.py) define reply protocols.
