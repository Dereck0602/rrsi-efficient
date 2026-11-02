# Algorithms

Choose a method by the resource you want to improve. FlashRSI supplies versioned resources, rollout, grading, and records; each method keeps its own proposal, selection, and memory policy.

## Instructions and skills

| Method | What it changes | How it searches | Settings / example |
| --- | --- | --- | --- |
| OPRO | Instructions | Proposes from scored history; returns the best candidate. | [Guide](../opro.md) · [中文](../opro.zh-CN.md) · [YAML](../../examples/opro.yaml) |
| GEPA | Instructions | Reflects on trajectories and screens a candidate pool. | [Guide](../gepa.md) · [中文](../gepa.zh-CN.md) · [YAML](../../examples/gepa.yaml) |
| EvoPrompt | Instructions | Uses GA or DE population search on dev scores. | [Guide](../evoprompt.md) · [中文](../evoprompt.zh-CN.md) · [YAML](../../examples/evoprompt.yaml) |
| ACE | Instructions / playbook | Accumulates feedback and reflections into a playbook. | [Guide](../ace.md) · [中文](../ace.zh-CN.md) · [YAML](../../examples/ace.yaml) |
| SkillOpt | Skills | Uses fast/slow updates and adopts strict dev gains. | [SkillOpt guide](../skillopt.md) · [Quickstart YAML](../../examples/quickstart.yaml) |
| Evo-Harness | Skills | Accumulates task-stream skills through native skill discovery. | [Guide](../evo-harness.md) · [中文](../evo-harness.zh-CN.md) · [YAML](../../examples/evo_harness.yaml) |

## Harness resources

These methods edit a broader resource scope. Start from their own configurations, including an Agent Worker capable of file edits.

| Method | How it searches | Settings / example |
| --- | --- | --- |
| AHE | Successive edits with diagnosis and edit audits. | [Guide](../ahe.md) · [中文](../ahe.zh-CN.md) · [YAML](../../examples/ahe.yaml) |
| ADAS | A design archive; retains proposals and selects the best. | [Guide](../adas.md) · [中文](../adas.zh-CN.md) · [YAML](../../examples/adas.yaml) |
| DarwinX | Branches and recombination with repeated outcome checks. | [DarwinX guide](../darwinx.md) · [YAML](../../examples/darwinx.yaml) |
| Meta-Harness | File-based history of code, scores, and traces. | [Guide](../meta-harness.md) · [中文](../meta-harness.zh-CN.md) · [YAML](../../examples/meta_harness.yaml) |
| ModularRSI | Independent module branches followed by integration. | [ModularRSI guide](../modular-rsi.md) · [YAML](../../examples/modular_rsi.yaml) |
| RRSI | History-guided revisions with score/cost admission. | [RRSI guide](../rrsi.md) · [YAML](../../examples/rrsi.yaml) |
| Arbor | Hypothesis-tree expansion, experiment selection, and upward insight synthesis. | [Arbor guide](../arbor.md) · [YAML](../../examples/arbor.yaml) |

## Match the Worker to the method

For three directory-editing methods, the current implementations differ as follows:

| Method | Editing base | Search evidence | Adoption and final selection |
| --- | --- | --- | --- |
| AHE | Current retained version | Train traces, optional debugger, previous verdicts | Keep every valid evaluated edit, including ties/regressions; return the last retained version. |
| ADAS | Current best-dev version | Design descriptions and dev scores, bounded in the prompt | Retain all measured designs in the archive; strictly higher dev changes best; promote best at the end. |
| Meta-Harness | Initial seed; Worker may copy any historical version | Historical source and full recorded dev/Worker traces in files | Retain all measured versions; strictly higher dev changes best; promote best at the end. |

These are local implementation rules, not claims of complete upstream reproduction. ADAS still has an unpinned upstream revision; the method pages state other adaptation limits.

- Use `algorithm.worker.type: llm` when the method requests textual proposals.
- For an Agent Worker, set `type: agent` and choose `name: miniagent`, `claude_code`, `codex`, `pi`, `hermes`, `openclaw`, or `opencode`, with independent `llm` settings.
- Evo-Harness needs directory-based native skill discovery. ModularRSI needs explicit module mappings. Follow the linked method guide before switching harnesses.

## Interpret results

The thirteen implementations adapt methods to FlashRSI's resource and evaluation interfaces. Registration is a configuration entry point; consult each guide's validation and migration notes for differences from the original method and tested paths. Arbor has a recorded Codex × WorkBench smoke with one task per split; see its [validation scope and limitations](../arbor.md#真实模型验证).

The paper's overall experiment table will be added when its final matrix is available. No aggregate result is claimed here.

To implement your own search policy, see [Add an algorithm](../development/algorithm.md).
