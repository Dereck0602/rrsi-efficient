# SkillOpt

[English](./skillopt.md) · **简体中文**

[算法总览](./integrations/algorithms.md) · [快速示例配置](../examples/quickstart.yaml)

SkillOpt 在两个时间尺度上改进技能文件。快速更新将一个训练 batch 中的成功与失败转化为小幅编辑；慢速更新比较不同 epoch 的版本，写入快速编辑无法覆盖的长期指导。两类资源更新都必须严格提高 dev 分数，才会被 Target 接受。

## 使用条件

- Target 必须声明 `skill` 组件。SkillOpt 读取并编辑其中的文本文件；评估时由原生 harness 执行完整 Snapshot。
- train 和 dev 都必须非空。训练轨迹提供反思证据，dev 分数决定是否接受候选。算法不读取 test 任务。
- Worker 可以是纯 LLM 或 Agent。两者都通过 `ask()` 返回 JSON 提案，由 SkillOpt 应用编辑，无需 Worker 具备文件编辑能力。

技能如何发现和加载由 harness 决定。更换 Target 前，请阅读 [Harness 接入说明](./integrations/harnesses.md)。

## 运行小示例

按[安装指南](./guide/installation.md)准备环境，然后在仓库根目录执行命令。在 [examples/quickstart.yaml](../examples/quickstart.yaml) 中分别配置 `agent.llm` 和 `algorithm.worker.llm`：两者各自需要模型与凭据，使用自定义端点时还需分别设置 `base_url`。

```bash
flashrsi run --config examples/quickstart.yaml --check-only
flashrsi run --config examples/quickstart.yaml
```

`--check-only` 检查初始化，不调用模型，也不验证远端认证。通过 shell 变量传入凭据的方法见[配置与 CLI](./guide/configuration.md)。

内置示例使用 MiniAgent、纯 LLM Worker 和 12 道本地 QA 题：train 6 题、dev 3 题、test 3 题。配置中的 `num_epochs: 1`、`max_steps: 1` 和 `batch_size: 2` 使其只执行**一次快速更新**，不覆盖慢更新或 meta-skill 生成。

要进入第二个完整 epoch 结束时的慢更新阶段：

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm.num_epochs 2 \
  --algorithm.max_steps null
```

这会增加实际模型调用。进入慢更新阶段不保证候选被接受或分数提高。这个小数据集适合观察流程；较完整的实验协议见 [SearchQA 复现记录](./searchqa-deepseek-reproduction.md)。

## 搜索与选择

1. 在 dev 上评估初始 Snapshot，将其分数保存为当前版本分数。
2. 使用 `seed + epoch` 打乱 train，再切成训练 batch。
3. 用当前 Snapshot 执行一个 batch。先将轨迹切成反思 minibatch，再在每个 minibatch 内划分成功与失败。成功要求状态为 `finished`，且主指标分数严格大于 `success_threshold`。
4. 对每个非空分组向 Worker 请求编辑建议。先分别合并同类提案，再通过优先修复失败的提示合并两类建议。如果结果超过 `edit_budget`，再让 Worker 排序并选择编辑。
5. 应用补丁，对发生变化的候选评估完整 dev。只有 `candidate_score > incumbent_score` 才接受候选，同分或下降均保留当前 Snapshot。最近 5 个被 dev 拒绝的快速补丁会进入后续反思提示。
6. 从第二个完整 epoch 结束时开始，在同一组采样的 train 任务上比较前一个 epoch 结束时的 Snapshot 与当前版本。按开关生成 meta-skill 记忆和慢速指导候选；指导候选经过与快速编辑相同的严格 dev 筛选。
7. 返回已接受的 Snapshot。最终 test 评估和实验输出由 runner 负责，详见[运行流程](./running-experiments.md)。

`max_steps` 统计跨 epoch 的训练 batch 数，不是模型调用次数。如果最后一个允许执行的 batch 恰好完成一个 epoch，该 epoch 仍可执行慢更新阶段；如果预算在 epoch 中途耗尽，则不为这个未完成的 epoch 执行慢更新。

## 技能中哪些内容会改变

快速提案支持 `append`、`insert_after`、`replace` 和 `delete`。除 `append` 外，每个操作都必须指定在文件中恰好出现一次的定位文本。只有 `append` 可以创建文件，路径必须满足 Target 的技能组件规则。

慢更新写入已有文本技能文件中的受保护区域，区域不存在时会创建：

```markdown
<!-- SLOW_UPDATE_START -->
长期指导
<!-- SLOW_UPDATE_END -->
```

快速编辑不能创建或修改这个区域，避免后续局部编辑覆盖跨 epoch 提炼的指导。

**Meta-skill 是 Worker 记忆**，在本次演化过程中提供给后续快速反思提示。它不写入 Target 的技能文件，也不单独经过 dev 筛选。`use_meta_skill` 和 `use_slow_update` 可以独立开关。

快速阶段中被补丁协议捕获的 JSON 或补丁格式错误会记录为 `invalid_patch`，并保留当前版本。空补丁和内容未变的候选不触发候选 dev 评估。Worker 执行失败会向外报错；慢更新与 meta-skill 的协议错误不在快速补丁恢复分支内。

## 参数与成本

以下是实现默认值，快速示例等 YAML 配置可以覆盖它们。所有字段均放在 `algorithm` 下。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `num_epochs` | `4` | 遍历 train 的次数；整数，至少 1。 |
| `batch_size` | `40` | 每次快速更新的训练任务数；至少 1。 |
| `reflection_minibatch_size` | `8` | 划分成功/失败前，每个反思分组的轨迹数；至少 1。 |
| `edit_budget` | `4` | 每次快速更新最多应用的编辑条数；至少 1。 |
| `merge_batch_size` | `8` | 每次合并调用处理的提案数；至少 2。 |
| `seed` | `42` | 用于打乱和慢阶段采样的整数随机种子。 |
| `max_steps` | `null` | 训练 batch 总上限；正整数或 `null`。 |
| `success_threshold` | `0.0` | 已完成轨迹的分数须严格大于此值，才进入成功分组。 |
| `use_slow_update` | `true` | 是否生成并评估受保护指导的更新。 |
| `use_meta_skill` | `true` | 是否生成供后续快速反思使用的优化记忆。 |
| `slow_update_samples` | `20` | 慢阶段比较的 train 任务数，不超过 train 大小；至少 1。 |
| `max_evidence_chars` | `30000` | 每份序列化证据的字符上限，不是整条提示的上限或 token 上限。 |

Target 开销包括初始 dev、每个训练 batch、发生变化的候选 dev，以及每次慢阶段比较中两个版本的样本评估。Worker 可能进行多次反思、合并、排序、meta-skill 和指导生成调用。因此一个 step 不对应固定调用预算；dev 大小和反思分组也会影响成本。

## 查看实验记录

结合候选 Snapshot 和任务轨迹，阅读保存的 `decision` 记录：

- `kind: fast` 或 `slow` 标识更新阶段。
- 经过评估的提案记录 `score`、`baseline_score`、`accepted`，以及父版本和候选的 ID。
- `invalid_patch`、`empty_patch` 和 `no_change` 解释未进入候选 dev 评估的更新。

例如，候选与基线同分意味着已经评估但被拒绝；格式错误的快速补丁则在候选 dev 评估之前被拒绝。这些结果的成本不同，都不应计作已接受的改进。

## 来源与适配

仓库的 [SearchQA 复现记录](./searchqa-deepseek-reproduction.md)将上游 `microsoft/SkillOpt` 固定为 commit `79124b37e9a6371e13b753f8bcd7adb1e493ade1`，并记录与论文的对照。本页描述本地实现，上游比较沿用该记录。

本地实现采用固定编辑预算、组件范围内的多文件 JSON 补丁、`seed + epoch` 打乱，以及先切 minibatch 再分成功/失败的顺序。快慢资源候选都经过严格 dev 筛选。复现实验还需要对齐模型设置、初始技能、数据成员、评分和恢复行为。

实现入口：[搜索主循环](../flashrsi/algorithms/skillopt/__init__.py)、[提示模板](../flashrsi/algorithms/skillopt/_prompts.py)和[补丁协议](../flashrsi/algorithms/skillopt/_patch.py)。

## 验证范围

[SearchQA 复现记录](./searchqa-deepseek-reproduction.md)包含 MiniAgent 和 Claude Code 上较完整的历史运行，以及协议差异和恢复尝试。这些结果对应各自记录的配置。

快速示例有意限制为一次快速更新。初始化检查不能证明真实模型运行成功，只覆盖快速更新的 smoke 也不能验证慢速指导或 meta-skill 行为。报告运行结果时，应注明算法、Target、Benchmark、预算以及实际执行的阶段。
