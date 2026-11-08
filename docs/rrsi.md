# RRSI

**English** · [简体中文](./rrsi.zh-CN.md)

[All algorithms](./integrations/algorithms.md) · [Example configuration](../examples/rrsi.yaml)

RRSI uses trajectory investigation and edit history to propose harness changes under a shrinking mechanism budget. Candidates must respect a historical score floor and a score/token-cost rule. Each round selects the highest-scoring admissible candidate, then search continues from that winner. The final result is the last accepted version, not necessarily the historical highest-scoring version.

## Requirements and a small run

Train must be non-empty with unique task IDs, and primary scores must be in `[0, 1]`. A separate file-editing Agent Worker is required (`algorithm.worker.type: agent`). Target and Worker have independent model and connection settings. The editable scope is every Target-declared component; semantic RRSI tags do not grant additional file permissions. Dev is unused; the runner evaluates test after final selection.

Follow [Installation](./guide/installation.md), configure both roles in [examples/rrsi.yaml](../examples/rrsi.yaml), then run from the repository root with bundled local tasks:

```bash
flashrsi run --config examples/rrsi.yaml \
  --benchmark.data_path ./examples/data/quickstart.jsonl \
  --benchmark.limit null \
  --benchmark.split_counts '{train: 3, dev: 1, test: 1}' \
  --algorithm.T 1 \
  --algorithm.m 1 \
  --algorithm.k 2 \
  --algorithm.m_draft 1 \
  --algorithm.n_fail_traces 2 \
  --algorithm.n_success_traces 1
```

This requests one round and one candidate, with two repetitions per full measurement. Investigation, review, repairs, and native agent turns can add many Worker calls. Append `--check-only` for initialization without model calls or remote authentication validation. Complete Target token usage is required during actual search; initialization cannot establish that accounting.

## Search and investigation

1. Measure the initial Snapshot on all train tasks `k` times. Retain task scores, trajectories, token costs, and optional weights/guard metrics. If `delta` is null, calibrate the noise band from baseline trials; otherwise use the configured value.
2. Rank tasks by their measured mean score. Choose up to `n_fail_traces` low-scoring tasks and their worst trial, then up to `n_success_traces` high-scoring tasks not already selected and their best trial. These labels indicate ranking, not a hard pass/fail threshold.
3. The analyst sees the task table and requests digests of representative traces. The digester reads a separate redacted trace file using Worker tools. Investigation supports failure, capability, and success perspectives plus follow-up questions; changing perspective does not expose a different trial. A report needs at least one digest.
4. Freeze a round context containing analysis, representative traces, the current score/cost, historical best score, edit budget, exploration/pruning directives, and previous edit history. All `m` sibling candidates start from the same current Snapshot and read this same context; one sibling does not learn from another's results during the round.
5. Propose each candidate, check and review its edit, then compile and execute a small native smoke. After proposals are generated, fully measure eligible candidates and apply the selection rules below. All comparisons use the saved incumbent measurement, not a fresh incumbent re-evaluation each round.
6. Record every edit's outcome and attribution after selecting the round winner. Advance the search version if a winner exists; otherwise retain the incumbent. Update the historical best score only from accepted incumbents. After `T` rounds, promote the final current version once.

Each full measurement submits all `N * k` task executions to one rollout queue.
They share the Target's `concurrency` limit across repeats, including environment
setup, agent execution, verification and cleanup. The default `task_order: fifo`
dispatches jobs in repeat/task order; the next repeat can start before the
previous repeat's slowest tasks finish.
One Snapshot and configuration are pinned for the whole queue; each execution
gets a private configuration, harness export and, for Harbor, a fresh trial and
environment. Task IDs and checksums are unchanged. After the queue completes,
grading and canonical records remain separate for each repeat, so repeated task
IDs cannot overwrite scores or token usage. Progress reports `N * k` executions
and identifies the repeat in each task's label. Restored baselines make no new
rollout calls; smoke and final test still execute each selected task once.

`task_order: longest_first` instead dispatches the longest predicted executions
first, across all repeats. Round 1 uses mean per-task baseline/calibration times;
each later round uses mean per-task times from all completed candidate
measurements in the previous round, including rejected candidates and errors.
All siblings share the same estimates, which are frozen until the next round.
Missing observations retain the last known estimate; tasks without any history
use the mean of known task estimates. With no history, dispatch remains FIFO.
Ties preserve canonical repeat/task order. Scores, evidence, and token accounting
are restored to canonical order before grading; scheduling never changes task
membership or repeat counts. `TaskExecutionTiming` records slot occupancy,
excluding queue wait. Older restored Harbor trials can supply their native
start/finish times from saved artifacts. Missing or invalid timing is ignored.
Measurement records include the planned `scheduling` order and per-trial
`execution_seconds`. Smoke and held-out timings do not train this scheduler.

Set `shared_candidate_queue: true` to share those same Target slots across all
eligible candidates in a round. The default is `false`, retaining serial
candidate evaluation; the TB2.1 DeepSeek example enables it. Proposals and smoke
checks finish first. The shared queue interleaves candidates at each slot of the
existing repeat/task schedule, using the same frozen historical estimates for
all candidates. `longest_first` prioritizes estimated slow tasks; ties retain
repeat/task order with candidates in generation order. Unknown durations use
the same fallback as above. Finishing one candidate does not trigger selection
or update the estimates while the queue runs.

Each job binds round, candidate index, immutable Snapshot, original task/checksum
and repeat. A private copy of the queue's frozen Target configuration accompanies
each existing adapter `rollout` call. Fresh exports and environments isolate jobs;
the global limit includes their cleanup, and queue waiting precedes execution
timers/deadlines. The queue checks returned task, `RunConfig` Snapshot identity
and any Harbor verifier identity before grouping by the full job key. Missing,
duplicate or mismatched results abort the batch after awaiting active cleanup;
ordinary task errors retain the existing zero-reward/reliability rules. Scores,
token costs and attribution are computed separately per candidate in generation
order after all jobs finish, preserving equal-score tie breaks and the original
selection rules. Repeated identical Snapshots still get independent trials.

A `candidate-queue` record saves the complete dispatch plan and concurrency;
per-candidate `measurement.scheduling` identifies its round and variant. Progress
reports the aggregate queue and includes candidate, Snapshot prefix and repeat.
Existing baseline and round-one resume modes permit changing this scheduling
flag and record its before/after values. They do not recover a partial shared
queue. Baseline, calibration, smoke and held-out evaluation keep their existing
execution paths. Interleaving can change model load and stochastic outputs;
this preserves the experiment's rules, not bitwise-identical model responses.

## Admission, ties, and final selection

Let S and C be incumbent score and mean Target token cost; S' and C' are candidate values, and S* is the highest score reached by accepted versions:

```text
dS = S' - S
dC = (C' - C) / C

if S' < S* - delta:
    reject
elif dS > delta:
    require dC <= beta0 + beta1 * dS
else:
    require w_s * dS - w_c * dC + w_n * novelty > 0

also require all configured guards to pass
```

Novelty counts distinct `client_tool`, `skill`, `memory`, and `subagent` tags absent from previously winning edits. Other tags do not earn this bonus. Tagging is checked against each declared edit's actual diff, with optional `component_signals` supplying additional regex evidence.

Among admissible candidates, choose the highest **score**, not the highest utility or lowest cost. Equal candidate scores retain the earlier generated candidate. The incumbent is not included in that tie comparison: a same-score candidate can replace it through lower cost or novelty. A slightly lower-scoring candidate can also replace it within the historical floor when the utility rule passes. If none qualifies, keep the incumbent.

`guards` constrain the unweighted mean change of named Benchmark metrics relative to the incumbent, using `max_drop` and/or `max_rise`. They apply in addition to the score/cost rule. An admissible candidate that loses to another candidate is recorded as `LOST`; only the winner contributes to accepted-component novelty history. Final selection returns the last accepted version without restoring the Snapshot associated with S*.

## Scores, failures, costs, and calibration

The score is an equal-weight mean over all train trials unless `weight_metric` names a strictly positive metric weight for every trial. Error-status trials receive reward zero and remain in the denominator. If their fraction exceeds `invalid_missing_frac` (default 15%), the measurement is invalid. Missing weights and missing guard metrics also invalidate it. An invalid baseline aborts search; an invalid candidate measurement is rejected. Worker output truncation is handled by the bounded recovery policy below; other Worker failures and framework protocol errors still propagate. Missing token accounting alone does not invalidate a measurement.

Cost is the mean token total per complete Target task execution, including cached tokens and reported internal auxiliary/subagent work, excluding evolution Worker calls. The reader supports native usage forms and prioritizes nonempty SDK `model_usage` session totals to avoid counting only the parent agent or double-counting totals. It cannot infer unreported usage. This is token cost, not money or latency.

For incomplete usage or a nonpositive mean, `cost` is null. `cost_accounting` separately records complete/partial/missing trial counts, observed tokens and their mean over **all** trials (a lower bound, not an estimate of missing consumption). If either side lacks a usable complete cost, acceptance requires a score gain strictly above `delta`, together with the historical score floor and domain guards. Cost savings or novelty alone cannot justify promotion in that comparison; `delta_C` is null. Complete accounting retains the original RRSI cost rule. This missing-cost fallback is a local robustness extension, not upstream-equivalent cost selection.

MiniAgent flushes reported usage to its native trace after each returned model call. On timeout or subprocess failure the adapter stops/reaps the child, recovers valid trace entries, and marks `accounting_complete: false`. Truncated trace tails are skipped. Calls interrupted before the provider returns usage remain unknown. Recovered usage is included in end-of-run totals, with an explicit count of incomplete records; the adapter does not fabricate zero usage or estimate unreported tokens.

With `delta: null`, default calibration bootstraps trials within each task 2,000 times and uses `delta_z * sqrt(2) * std(bootstrap scores)`. With multiple baseline measurements, a positive sample standard deviation supplies the band; otherwise pooled within-task bootstrap is rescaled to the original evaluation size. Calibration retains the first baseline as incumbent rather than selecting the best calibration run. Automatic calibration requires `k >= 2` or `calibration_evals >= 2`.

The runner's final test score remains the primary metric's arithmetic mean under the framework contract. It is not an extra weighted RRSI test score; weighted search scores and that final mean may represent different objectives.

## Edit budget, review, and repairs

For zero-based round t, the independent-mechanism budget is:

```text
ceil(b_min + (b_max - b_min) * (1 + cos(pi * t / T)) / 2)
```

The implementation rounds the intermediate value to nine decimal places before taking the ceiling. Rounds use `t = 0 ... T-1`, so the final round need not reach exactly `b_min`. One mechanism can span several files; the budget is not a file count. After `w` rounds without score gain beyond `delta`, the last `m_draft` slots must exercise an untried semantic component if any remain. Prune directives identify components without positive measured gain in the recent `n_prune` window; they are suggestions for reviewed deletion proposals, not automatic deletion.

`<EDITS>` contains JSON describing independent mechanisms with `id`, `component`, `hypothesis`, `targets_mode`, `predicted_affected`, `retroactive_check`, and `paths`; optional `operation` is `change` or `prune`. IDs must be unique and paths must cover the actual cumulative diff. Supported tags are `prompt`, `control_flow`, `config`, `output_plumbing`, `context_mgmt`, `client_tool`, `skill`, `memory`, and `subagent`.

Each candidate gets an initial edit plus at most `repair_rounds` repairs in the same checkout. The Worker reads `context.json` and `current-diff.txt`; repaired declarations must account for all changes relative to the incumbent. Snapshot constraints, empty edits, credential-pattern checks, binary-diff rejection, critic objections, and reserved-slot compliance are handled before smoke. The critic reviews the full diff and returns a `<REVIEW>` verdict. Unparseable critic replies are retried up to three calls, then treated as rejection.

Explicit Worker generation truncation (`length` or `max_output_tokens`) restarts the affected phase at most `worker_output_retries` times (default 1, meaning two total attempts). The phases are a round's analysis, including its digests, and each candidate's proposal, including repairs and critic review. Each candidate restart uses a fresh incumbent checkout, discarding partial edits. Each analysis restart discards the incomplete report and waits for active digester cleanup. A retry receives a reminder to take concrete actions and return a concise structured response. Model settings, reasoning mode, per-response token limits and per-Worker-call timeouts stay unchanged; each fresh call can consume its normal time and token allowance. Exhausted candidate attempts produce an unmeasured rejected candidate; exhausted analysis attempts skip that round while retaining the incumbent and best score. Later candidates and rounds continue. Set `worker_output_retries: 0` to skip immediately. Context overflow, authentication, infrastructure errors and cancellation are not converted into output-budget skips.

Failed and retried Worker trajectories retain their observed usage. `worker-output-budget` records the phase, round index, candidate index, attempt, error and retry/skip action. An analysis skip is also marked in `rrsi-context` and `round-summary`; a candidate skip is recorded in `edit-history` without a fabricated score.

Once those gates pass, compile all candidate `.py` files and execute up to `smoke_n` selected representative train tasks in train order. Error status fails the smoke; score improvement is not required here. `smoke_n: 0` omits execution but still compiles. A compile or smoke failure returns an invalid candidate directly; it does not re-enter the same candidate's repair loop. Temporary evidence files are removed before resource commit.

Digest quotes must match a substring of a decoded trace value, and the trace file must remain unchanged. Each digest has a 6,000-character limit and up to three protocol attempts. Analyst interaction is capped at 30 turns; these are protocol constants, not configurable search budgets.

## Parameters and cost

`digest_concurrency` (default 1) bounds concurrent independent digest investigations
within one analyst action (up to 8 requests). Results retain request order and each
digester has its own evidence directory and Worker execution context. Analyst
decisions and follow-up batches remain sequential; failure/cancellation awaits
all active Worker cleanup before returning. The `digests` progress bar reports
this concurrency separately from the sequential evolution `rounds` counter.

Defaults belong under `algorithm`. RRSI rejects unknown algorithm options.

| Field | Default | Meaning |
| --- | --- | --- |
| `T` | `20` | Search rounds, integer at least 1. |
| `k` | `2` | Repetitions per full train measurement, integer at least 1. |
| `task_order` | `fifo` | Dispatch in canonical order or `longest_first` using previous-round task durations. |
| `shared_candidate_queue` | `false` | Share Target concurrency across eligible candidates; restore canonical results before selection. |
| `m` | `2` | Sibling candidate slots per round, integer at least 1. |
| `b_min` | `1` | Minimum mechanism-budget parameter, integer at least 1. |
| `b_max` | `4` | Initial mechanism-budget parameter, integer at least `b_min`. |
| `w` | `3` | Stagnation lookback in rounds, integer at least 1. |
| `m_draft` | `1` | Reserved exploration slots when stalled, integer in `[0, m]`. |
| `delta` | `None` | Nonnegative score noise band; null enables calibration. |
| `delta_z` | `2.0` | Nonnegative calibration multiplier. |
| `beta0` | `0.1` | Nonnegative base relative-cost allowance for clear gains. |
| `beta1` | `40.0` | Nonnegative extra cost allowance per unit of clear score gain. |
| `w_s` | `100.0` | Nonnegative within-band score weight. |
| `w_c` | `15.0` | Nonnegative within-band relative-cost penalty. |
| `w_n` | `0.5` | Nonnegative structural novelty weight. |
| `n_prune` | `4` | Recent-round pruning window, integer at least 1. |
| `repair_rounds` | `5` | Extra proposal repair attempts, integer at least 0. |
| `worker_output_retries` | `1` | Extra phase attempts after Worker output truncation, integer at least 0; exhausted attempts skip the candidate or round. |
| `invalid_missing_frac` | `0.15` | Maximum error-trial fraction, in `[0, 1]`. |
| `seed` | `7` | Integer bootstrap seed; does not control model sampling. |
| `n_fail_traces` | `22` | Low-score representative task count, integer at least 0. |
| `n_success_traces` | `6` | High-score representative task count, integer at least 0; total requested representatives must be positive. |
| `calibration_evals` | `1` | Total baseline measurements when auto-calibrating, integer at least 1. |
| `smoke_n` | `2` | Maximum representative tasks per candidate smoke, integer at least 0. |
| `component_signals` | `{}` | Semantic tags mapped to nonempty regex lists; not file permissions. |
| `weight_metric` | `None` | Optional positive per-trial weighting metric. |
| `guards` | `{}` | Metric names mapped to nonnegative `max_drop`/`max_rise` limits. |

For N train tasks, full measurement cost is `N * k` Target task executions. Baseline/calibration costs `N * k * calibration_evals` when delta is automatic, otherwise `N * k`. Full candidate measurements cost at most `T * m * N * k`; candidate smokes add at most `T * m * min(smoke_n, N, n_fail_traces + n_success_traces)`. Invalid proposals can reduce these counts. Final test, retries, and native model/tool turns are additional.

Worker cost is separate: each candidate phase attempt has at most `repair_rounds + 1` editing calls, with up to three critic calls per review reached. Output truncation can restart the phase for at most `worker_output_retries + 1` total attempts. Each round also includes analyst and digest calls; a digest request can investigate up to eight traces, and the analysis phase has the same restart bound. The mechanism budget does not cap all these API requests.

## Records and adaptation

`measurement` records scores, costs, trial evidence and guard metrics; `calibration` records the noise band. `rrsi-context` freezes the common round evidence. `critic`, `repair`, and `smoke-gate` explain proposal handling. `edit-history` retains full diffs, `admissible`, `accepted`, `ACCEPTED/LOST/REJECTED`, score/cost changes, and attribution. Final `promote` includes final score/cost, historical best score, and delta.

Attribution checks predicted tasks against per-task mean changes and marks unpredicted drops of at least `1/k`. Unknown predicted task IDs are excluded from the hit-rate denominator. Bundled edits share a candidate measurement; attribution is not a causal ablation of each mechanism. Invalid or unmeasured candidates retain reasons but do not count as measured component attempts or gains.

The [implementation](../flashrsi/algorithms/rrsi/__init__.py) independently ports mechanisms from [google-research/rrsi](https://github.com/google-research/rrsi/tree/be50316e1db05914068a973f322770ef08ed7ba1), pinned to `be50316e1db05914068a973f322770ef08ed7ba1`. Agent, Benchmark, Worker, Snapshot and Algorithm supply execution, grading, edits, versioning and records. Upstream Git management, Domain adapters, role-specific model configuration, resume/re-evaluation commands and starting harnesses are not replicated. The SearchQA/MiniAgent example is a local adaptation, not the original coding experiment configuration.
