# EvoPrompt

**English** · [简体中文](./evoprompt.zh-CN.md)

[All algorithms](./integrations/algorithms.md) · [Example configuration](../examples/evoprompt.yaml)

EvoPrompt evolves a population of instructions using genetic algorithm (GA) or differential evolution (DE) prompts. GA selects survivors after producing a whole generation. DE compares each trial only with the member at its target position. Both variants retain the best dev version seen across generations for final promotion.

## Requirements

The Target must declare an `instruction_file` in its `prompt` component. The Worker can be a text LLM or an Agent returning proposals through `ask()`. Operators replace the instruction's complete text while preserving the rest of the Snapshot.

Dev must be non-empty. Population size must be at least 2 for GA and 4 for DE. This implementation evaluates fitness on dev and does not consume train trajectories. The runner owns final test evaluation.

## Run a small example

Follow [Installation](./guide/installation.md). Independently configure the Target and Worker model settings in [examples/quickstart.yaml](../examples/quickstart.yaml), then use its bundled local data from the repository root:

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm evoprompt \
  --algorithm.variant ga \
  --algorithm.population_size 4 \
  --algorithm.max_steps 1
```

For one DE generation, keep the population size at 4 or more:

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm evoprompt \
  --algorithm.variant de \
  --algorithm.population_size 4 \
  --algorithm.max_steps 1
```

Append `--check-only` for initialization without model calls. It does not validate model authentication or either operator. The method-specific [SearchQA configuration](../examples/evoprompt.yaml) uses larger defaults. See [Configuration & CLI](./guide/configuration.md) for credentials and overrides.

## Search and selection

1. Evaluate the initial Snapshot on dev. Fill the remaining population slots with Worker paraphrases of its instruction, evaluating each changed instruction on full dev.
2. For each generation, run the selected GA or DE operator with randomness seeded by `seed + step`.
3. Update the live population using the variant's survivor policy, and retain a separate global best. Replace that best only on a strictly higher dev score, so ties keep the previously selected best.
4. After `max_steps` generations, promote the global best once. The active Target Snapshot does not move during population search.

### GA

Generate `population_size` children from parent pairs. `wheel` first resamples the population with replacement, weighted by fitness; all-zero fitness uses uniform weights. `random` draws pairs uniformly. `tournament` selects each parent by taking the fitter of two sampled members; `tour` is an alias.

With `survivor_selection: topk`, rank the original parents and children together, retain the best distinct instructions, then pad with the best remaining entries if needed to keep the population size. With `standard`, all children become the next generation. The separate global best survives even when `standard` removes it from the live population.

### DE

For each target position, sample three distinct donor positions from the other members. The prompt combines donor differences with a base instruction and the target. By default, the base is the current generation's best member; `donor_random: true` uses the third sampled donor. All trials use the generation's original population.

A trial replaces only its own target and only on a strictly higher dev score. A tie retains that target. GA's `selection` and `survivor_selection` do not control DE.

## Parameters and cost

All fields belong under `algorithm`. Defaults are larger than the small examples above.

| Field | Default | Meaning |
| --- | --- | --- |
| `variant` | `ga` | `ga` or `de`. |
| `population_size` | `10` | Number of slots; at least 2 for GA, 4 for DE. |
| `max_steps` | `10` | Generations after initialization; integer, at least 1. |
| `selection` | `wheel` | GA parent policy: `wheel`, `random`, or `tournament` (`tour` alias). |
| `survivor_selection` | `topk` | GA survivors: `topk` or `standard`. |
| `donor_random` | `false` | DE base: random third donor instead of generation best. |
| `seed` | `42` | Integer seed for generation-level parent/donor sampling. |

For population size P, G generations, and dev size D, search costs at most `P * (1 + G) * D` Target task executions. Worker calls total `(P - 1) + P * G` on normal completion: initialization paraphrases plus generation proposals. Final test and native model/tool turns are additional.

An empty reply or an instruction identical to its parent after line-ending normalization carries the parent's Snapshot and score without another dev pass. This still fills a population slot. Other duplicate instructions are not globally cached, so they can incur another evaluation.

## Inspect an experiment

Each `generation` decision records `variant`, `step`, proposed `candidates`, and the surviving `population`. Member rows contain instruction text, score, and Snapshot ID. `carried: true` identifies a reused parent result, not a newly evaluated tie.

The final `promote` record supplies selected and baseline scores plus the final population. Under standard GA, the selected global best can be absent from that final live population. Read the promotion's selected Snapshot rather than assuming the last generation contains the winner.

## Source and adaptation

The [implementation](../flashrsi/algorithms/evoprompt/__init__.py) pins `microsoft/EvoPrompt` to commit `2e235c9410e38d531dabfae7fdd9cbded1577373`. The local port evolves one native instruction file through FlashRSI Snapshots and full-dev fitness. The [prompt module](../flashrsi/algorithms/evoprompt/_prompts.py) contains initialization, GA, and DE templates; replies use `<prompt>...</prompt>`.

The preserved population and operator mechanisms do not imply reproduction of upstream model settings or experimental results. In particular, fixed population size can include duplicate or carried members, while final selection preserves the best dev incumbent across generations.
