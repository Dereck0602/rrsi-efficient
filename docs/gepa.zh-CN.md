# GEPA

[English](./gepa.md) · **简体中文**

[算法总览](./integrations/algorithms.md) · [示例配置](../examples/gepa.yaml)

GEPA 反思父版本的训练轨迹，在相同任务上筛选重写后的指令，并保留候选的逐题 dev 分数。选择父版本时，即使某个候选平均分不最高，只要它在个别 dev 题目上领先，仍可能继续参与搜索。最终按 dev 均分选优，同分保留较早候选。

## 使用条件

Target 必须声明属于 `prompt` 组件的 `instruction_file`。Worker 可以是通过 `ask()` 返回重写指令的纯 LLM 或 Agent。

train 和 dev 必须非空，dev 的任务 ID 必须唯一。`use_merge: true` 会在启动时报错，因为尚未实现 system-aware merge。最终 test 评估由 runner 负责。

## 运行小示例

按[安装指南](./guide/installation.md)准备环境，在 [examples/quickstart.yaml](../examples/quickstart.yaml) 中独立配置两个角色的模型设置。从仓库根目录运行：

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm gepa \
  --algorithm.num_epochs 1 \
  --algorithm.reflection_minibatch_size 2 \
  --algorithm.max_metric_calls 10
```

内置划分包含 train 6 题、dev 3 题。该预算足够执行初始 dev 加一次完整迭代：`3 + 2 * 2 + 3 = 10` 次任务评估。提前拒绝可能为更多迭代留下预算。追加 `--check-only` 可仅检查初始化。GEPA 使用 `max_metric_calls` 和 `num_epochs`，不使用 quickstart 中的 `max_steps` 字段。

使用 HotpotQA context 模式时，从 [examples/gepa.yaml](../examples/gepa.yaml) 及其数据前置条件开始。连接设置见[配置与 CLI](./guide/configuration.md)。

## 搜索与选择

1. 在 dev 上评估初始 Snapshot，将均分和逐题分数存入种群。
2. 遍历打乱后的 train minibatch。按 `candidate_selection_strategy` 选择均分最佳父版本，或从逐题前沿采样。
3. 在 minibatch 上评估父版本。默认从反思证据中排除分数恰为 `1.0` 的轨迹；若没有剩余证据，以 `perfect_minibatch` 拒绝本次迭代。
4. 让 Worker 根据父版本输出、带标签的反馈和执行消息重写指令。拒绝空指令、未改变指令或已出现的指令；去重时规范化换行符及末尾换行。
5. 在同一 minibatch 上评估候选。默认要求严格超过父版本；`improvement_or_equal` 还允许同分通过这一步筛选。
6. 对通过筛选的候选评估完整 dev，即使其 dev 均分更低也加入种群。进入种群不等于最终提升。
7. 任务评估预算或 epoch 上限结束搜索后，提升种群中最早出现的最高 dev 均分版本。

Pareto 选择将每道 dev 题映射到该题最高分成员，在保留覆盖的前提下裁掉冗余成员，再从剩余成员关系中采样。领先题目越多的成员，采样权重越大。这个前沿决定父版本资格，完整的已评估种群仍保留记录。

## 参数与成本

所有字段均放在 `algorithm` 下。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `max_metric_calls` | `null` | Target 任务评估预算；正整数或自动计算。 |
| `num_epochs` | `null` | train 遍历次数上限；正整数或仅按预算搜索。 |
| `reflection_minibatch_size` | `3` | minibatch 大小，不超过 train 大小；至少 1。 |
| `skip_perfect_score` | `true` | 从反思中排除分数为 1.0 的轨迹。 |
| `acceptance_criterion` | `strict_improvement` | minibatch 筛选规则：`strict_improvement` 或 `improvement_or_equal`。 |
| `candidate_selection_strategy` | `pareto` | 父版本选择：`pareto` 或 `current_best`。 |
| `max_evidence_chars` | `24000` | 每份序列化证据块的字符上限。 |
| `seed` | `42` | 训练顺序和父版本采样的整数随机种子。 |
| `use_merge` | `false` | 必须为 false，尚不支持 merge。 |

设 train 大小为 N、dev 大小为 D，B = `min(reflection_minibatch_size, N)`。基线消耗 D 次评估，完整迭代消耗 `2 * B + D` 次。启动时预算必须足以覆盖两者。每次迭代开始前检查是否容得下完整开销，实际只累计已执行的评估。

未显式设置预算时，预算为 `max(100, D + I * (2 * B + D))`。仅按预算搜索时 I 为 10；指定 epoch 时，I 为 `num_epochs * (N + B - N % B) / B`。每个打乱的 epoch 使用调用次数最少的任务索引补齐，即使 N 能被 B 整除，也额外补一个完整 minibatch。因此自动预算不保证恰好执行十次迭代。

Worker 调用、原生模型/工具轮次和最终 test 不计入这个任务评估预算。允许 minibatch 同分通过，不改变最终 dev 同分处理。

## 查看实验记录

迭代决策区分 `reject` 和 `admit`。拒绝原因包括 `perfect_minibatch`、`empty_proposal`、`unchanged_instruction`、`duplicate_instruction` 和 `failed_minibatch_screen`。`admit` 记录中的 `accepted: true` 表示加入种群，不表示成为最终 Target。

最终 `promote` 记录包含 `metric_calls`、`iterations`、`acceptance_criterion`、`tie_break`，以及带逐题分数和父版本索引的种群。用它确认选中的版本和实际评估预算。

## 来源与适配

[搜索模块](../flashrsi/algorithms/gepa/__init__.py)将采样参考 `gepa-ai/gepa-artifact` 固定在 `cbefbc1aa0f43dd39874ec4bf42211365dbda42e`。[种群模块](../flashrsi/algorithms/gepa/_population.py)将 `gepa-ai/gepa` 固定在 `0632cdb5dcc052e690eab439e1b4a7e3e9cfe407`。

本地适配重写一个 harness 指令文件，不包含复合系统的模块轮换，也未实现 system-aware merge。反思使用 FlashRSI 的训练反馈和轨迹；[提示模板](../flashrsi/algorithms/gepa/_prompts.py)定义回复格式。
