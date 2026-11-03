# ModularRSI

[English](./modular-rsi.md) · **简体中文**

[算法总览](./integrations/algorithms.md) · [MiniAgent 配置](../examples/modular_rsi.yaml) · [Claude Code 配置](../examples/modular_rsi_claude_code.yaml)

ModularRSI 从同一个初始版本独立演化各模块，再集成并继续改进。它对同一道 train 任务重复执行，用成功与失败轨迹诊断问题。分支是否前进由文件检查、diff review 和执行门禁决定，不设 train/dev 涨分门禁，也不维护最高分档案。只要通过这些检查，候选即使同分或降分，也可以替换父版本。

## 使用条件

train 必须非空，任务 ID 唯一且与 dev/test ID 不重合。dev 不参与诊断、候选选择或 Worker 提示。Runner 对最终选中的 Snapshot 执行 test。算法使用当前 `benchmark.train`，独立外部演化数据集是可选项。

Worker 必须是独立的 Agent 实例，配置 `algorithm.worker.type: agent`。编辑 Target checkout 时，它保留自己的 harness 资源、模型与连接配置。Target 和 Worker 可以使用不同 harness。Target 活动 Snapshot 只在最终提升时切换，故障候选不会替换 Worker 的执行资源。

`algorithm.modules` 必须完整声明下述五个运行时模块或三个扩展模块。每个模块映射到非空的现有 Target Snapshot 文件列表。`evolve_scope = {"*"}` 总体允许 Target 组件编辑，模块文件列表进一步限制每次修改范围。

## 模块边界

MiniAgent 的五个功能模块可按原生文件映射：

```yaml
modules:
  agent_loop: [runtime/agent.py]
  tool_use: [runtime/agent.py, tools/shell.py]
  observation_management: [runtime/agent.py, runtime/tool_result.py]
  context_management: [runtime/agent.py]
  task_completion_detection: [runtime/agent.py]
```

多个模块可以共用一个文件。框架强制约束的是**文件边界**；文件内部的功能职责靠提示与 Worker review 约束，不是 AST 隔离或函数级权限。不接受 `文件.py::函数`。允许在已声明文件内新增或删除函数、签名、decorator；若需要新的文件路径，必须先在初始资源映射中准备好。

Claude Code 提供原生扩展资源，不开放闭源运行时源码。其三模块配置为：

```yaml
modules:
  prompt: [CLAUDE.md]
  skill: [skills/workflow/SKILL.md]
  hook: [hooks/hooks.json]
```

[Claude Code 示例](../examples/modular_rsi_claude_code.yaml)通过 `source_dir` 提供这些文件和原生插件资源，并使用独立编辑 Worker。这些是扩展分支，不等价于替换五种运行时机制。文件列表是精确路径，不是目录通配符。切换 harness 时要同时提供匹配的资源目录和模块映射，不会根据 harness 名称自动推断。

## 运行小示例

按[安装指南](./guide/installation.md)准备环境，并在 [examples/modular_rsi.yaml](../examples/modular_rsi.yaml) 中独立配置 Target 与 Worker。该示例的 Target 和模块映射使用 MiniAgent。若希望用仓库自带的本地 SearchQA 任务替代示例中的 Terminal-Bench 任务，从仓库根目录运行：

```bash
flashrsi run --config examples/modular_rsi.yaml \
  --benchmark searchqa \
  --benchmark.data_path ./examples/data/quickstart.jsonl \
  --algorithm.num_epochs 1 \
  --algorithm.integration_epochs 1 \
  --algorithm.batch_size 1 \
  --algorithm.num_rollouts 2 \
  --algorithm.validation_samples 1
```

示例原有的 `split_counts: {train: 1, dev: 1, test: 1}` 继续生效。这仍会演化五个分支并进行集成，所以一个 epoch 不等于一次 Worker 编辑。追加 `--check-only` 可检查初始化，不调用模型，也不验证远端认证。运行原生 Terminal-Bench 或 SWE 任务时需配置相应数据与执行环境，见 [Terminal 配置](../examples/modular_rsi.yaml)和 [SWE 配置](../examples/modular_rsi_swe_bench.yaml)。

## 分支演化与准入

1. 各模块分支从同一个初始 Snapshot 出发，按配置顺序依次演化，不继承其他模块的中间修改或成功记忆。所有打乱和执行检查采样共用一个设定种子的随机数生成器。
2. 每个 epoch 打乱 train 并分 batch，用当前分支重复执行每批任务 `num_rollouts` 次。只有轨迹状态为 `finished` 且主指标达到 `success_threshold`，才记为成功。
3. 按任务分组：同时出现成功和失败为 `contrastive`，全部成功为 `positive`，没有成功为 `negative`。当前失败任务可附上本分支此前访问该任务时最近保存的成功轨迹。提示分别要求同题对比、失败诊断或效率改进。
4. Worker 输出 findings。针对同一文件的建议合并，按不同支持任务 ID 的数量排序，最多保留 `edit_budget` 个文件目标。同数时保留回复中首次出现的顺序。空 findings 列表会跳过该批编辑。
5. 围绕选中的建议执行一次目录编辑。检查实际改动路径，编译修改后仍存在的 `.py` 文件，并拒绝空编辑。随后 review 完整 diff，包含可执行位变化；diff 超出证据预算就拒绝，不审查截断补丁。
6. Worker 接受 diff 后，从当前 batch 随机取最多 `validation_samples` 道任务，各执行一次候选。所有返回轨迹都必须为 `finished`。仍会得到分数，但门禁不要求答案正确或均分提升。
7. 通过这些检查才更新分支，否则保留前一版。已接受的模块修改写入该分支历史。完成全部 batch 和 epoch 后，保留各分支最后一个已接受版本。

这里没有分数平局规则：同分候选可以前进，降分候选也可以前进。`success_threshold` 控制证据分组，不控制准入。主指标不必限定在 `[0, 1]`，阈值应按该指标的单位设置。

## 集成与最终选择

独立分支演化结束后，Worker 编辑一个全新的 seed checkout，同时读取各模块最终分支的临时 checkout 和已接受修改历史。共享文件中的改动通过普通文件编辑集成；框架不切片拼接函数，也不会机械地用一个分支覆盖另一个分支。参考目录在提交前移除。

初始集成编辑执行相同的文件范围、编译和 diff review 门禁。若被拒，则以 seed 作为合并版本。随后在**完整 train** 上执行合并版本；通过则将它设为回退版本，否则回退版本为 seed。

集成改进仍从合并版本出发，即使刚才的全量执行检查失败，也允许独立 Worker 尝试修复。此阶段运行 `integration_epochs` 轮对比式 batch 评估，每批直接请求集成编辑，不单独调用 findings 分析。可改范围为所有模块声明文件的并集，每次仍可读取原始分支 checkout，并经过 diff review 和抽样执行检查。

最后的改进版本必须通过一次全量 train 执行检查；失败则返回回退版本。若初始合并版已执行失败、后续又始终没有改变，则直接回退，不重复最终检查。这是固定的回退顺序，不比较 train/dev 分数，也不从所有集成中间版本中挑最高分。该回退机制不会额外保证 seed 一定可执行。

## 证据、回复格式与失败处理

findings 必须是 JSON 数组。每项严格包含 `target`、`task_ids`、`rationale`、`change`：目标必须是当前模块声明文件，引用 ID 必须属于当前 batch，原因与建议为非空字符串。review 必须严格返回包含布尔值 `accept` 和非空字符串 `reason` 的 JSON 对象。解析器允许最外层 Markdown JSON 围栏，但提示要求纯 JSON。非法 JSON 或字段协议错误会中止运行，不会静默丢弃 findings。

完整对比轨迹保留在实验记录与分支记忆中。提示证据过长时，保留每题的分组、计数、每种已有正负类别的一个近期样本，以及历史成功，再缩短文本字段。连这些结构都容纳不了则报错。普通历史与 findings 使用共享字符上限。集成阶段接收各模块历史，不把集成编辑追加到这些模块历史里。

文件/语法错误、超预算 diff 会拒绝编辑，合法 review 返回 `accept: false` 也会拒绝。Worker 失败向外传播。对比评估将 `ValueError`、`TypeError`、`KeyError`、`AttributeError` 记录为整批失败证据，不声称已知出错的是哪道题；执行门禁将相同异常视为失败，其他异常仍可能向外传播。被拒 batch 编辑没有立即重试循环，但后续 batch 仍可诊断并再次编辑。

## 参数与成本

实现默认值放在 `algorithm` 下。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `modules` | `{}` | 必填的完整五运行时模块或三扩展模块到现有文件的映射；空默认值无法运行。 |
| `num_epochs` | `3` | 每个独立模块的 epoch 数；整数，至少 1。 |
| `batch_size` | `10` | 每批 train 任务数；整数，至少 1。 |
| `num_rollouts` | `3` | 每题用于对比证据的重复执行次数；整数，至少 2。 |
| `integration_epochs` | `1` | 合并 harness 的后续改进 epoch 数；整数，至少 1。 |
| `validation_samples` | `2` | 每个候选从当前 batch 抽取的不同执行检查任务数；整数，至少 1，不超过该批大小。 |
| `edit_budget` | `1` | 合并 findings 后优先处理的文件目标数上限；整数，至少 1。不是编辑尝试次数，也不是模块内实际改动文件数的硬限制。 |
| `success_threshold` | `1.0` | 对比证据中判为成功的主指标阈值。 |
| `seed` | `42` | train 打乱和执行检查采样的整数随机种子，不控制模型采样。 |
| `max_evidence_chars` | `60000` | 每个证据块的字符上限；整数，至少 1。完整 diff review 必须能放下。 |

设模块数 M 为 5 或 3，train 大小为 N，B = `ceil(N / batch_size)`，E = `num_epochs`，I = `integration_epochs`，R = `num_rollouts`，V = `min(validation_samples, batch_size)`。

| 工作 | Target 任务执行数 / Worker 调用数 |
| --- | --- |
| 全部分支和集成的对比评估 | 完成运行时执行 `(M * E + I) * N * R` 次 Target 任务 |
| 候选抽样执行检查 | 最多额外 `(M * E + I) * B * V` 次 Target 任务 |
| 初始合并版和最终版的全量检查 | 最多额外 `2 * N` 次 Target 任务 |
| 独立模块的每个 batch | 一次分析；最多一次编辑和一次 review |
| 初始合并 | 一次编辑，文件检查通过后再做一次 review |
| 集成改进的每个 batch | 最多一次编辑和一次 review，不单独分析 |

最后一批可能少于 V 道任务。空 findings 和被拒编辑会减少执行门禁与 review 次数。最终 test、原生模型/工具轮次、后端重试及环境准备另计。`edit_budget` 只截取优先 findings，增加它不会让每批执行多次编辑尝试。

## 演化数据

默认使用所配置数据集的 train 划分，不要求 `metadata.evolution_source`。不同 ID 的任务可以共享 policy 文本或环境目录。各 Benchmark 加载器保留自身数据检查；算法本身检查 train ID 唯一并与 dev/test ID 隔离。

TerminalBench 与 SWEBench 可选配置 `benchmark.evolution_data_path` 和 `evolution_limit`，用外部任务集合替换 train，保留已配置的下游 dev/test。TerminalBench 接受原生 Harbor 任务；SWEBench 接受带任务镜像、基线 commit 和评分必需字段的 SWE 记录，不能填 Harbor 目录。二者继续使用原生加载、执行与评分。

显式外部模式额外检查 ID、任务文本和 checksum 重合，SWEBench 还拒绝共用任务镜像。`evolution_limit` 独立限制外部池，下游仍由 `limit` 控制。示例使用绝对数据路径；若使用相对路径，遵循[配置与 CLI](./guide/configuration.md)及对应 Benchmark 的路径规则。前面的本地示例不需要外部来源。

## 查看实验记录

`contrast` 保留每次重复评估，`contrast_groups` 保存任务分组，`contrast_error` 保存整批协议失败。`review` 记录模型的接受与理由，`gate` 记录执行是否完成或捕获的错误。决策标明所属模块及编辑被接受或拒绝；review 接受不代表后续执行门禁已经通过。

最终 `promote` 记录选中候选，以及模块名到最终分支 Snapshot ID 的 `modules` 映射。选中的集成 Snapshot 可能与任何单独模块分支都不同。临时参考目录不进入候选资源。

## 来源与适配

[实现](../flashrsi/algorithms/modular_rsi/__init__.py)固定引用 [IQuestLab/ModularRSI](https://github.com/IQuestLab/ModularRSI/tree/b5c72c36b0d08ff93f00ee202a8fbdebe849dfb9) commit `b5c72c36b0d08ff93f00ee202a8fbdebe849dfb9`，参考 `src/harbor/agents/terminus_2_modular/self_evo/`。本地代码与提示围绕 FlashRSI 接口独立编写。

- 原生 harness 文件替代上游可发现函数库及按任务组合的运行时 Function Composer，未提供函数级权限、discovery-contract 强制检查或 self-attribute audit。
- 三扩展模块模式用于 Claude Code 等 harness，不会开放五种运行时机制的源码。
- 分支顺序执行。集成通过 Worker 文件编辑和 review 完成，包括共享文件冲突处理及可选的冗余函数删除，没有独立 Function Merge 阶段。
- 默认允许同源 train/dev/test 划分，不强制采用上游独立演化池；可显式配置外部数据。
- Python 检查编译修改源码，不在宿主 import 候选。diff review 是模型判断，不是独立反作弊证明。执行与最终选择遵循上述本地门禁，不做 dev 分数优化。
