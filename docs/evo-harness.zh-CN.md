# Evo-Harness

[English](./evo-harness.md) · **简体中文**

[算法总览](./integrations/algorithms.md) · [示例配置](../examples/evo_harness.yaml)

Evo-Harness 单次消费训练任务，将失败编译为两级原生技能：面向特定主题的程序，以及跨任务的通用指导。同一 batch 使用固定 Snapshot，整批结束后才编译。合法变化持续采用，不设 dev 门槛。最终 Snapshot 可通过普通 Agent 执行，推理时不需要优化器。

## 使用条件

Target 必须声明 `skill` 和 `config` 组件，并原生支持目录技能的发现与加载。算法只编辑 `skill`，`config` 只读。声明组件本身不会为自定义 harness 添加技能加载器。

train 必须非空且任务 ID 唯一。Worker 必须是可编辑文件的 Agent（`algorithm.worker.type: agent`），纯 LLM Worker 会在启动时报错。Target 和 Worker 的模型与连接配置分别填写。

算法不做 dev 选优，不重复 epoch，也不重试当前任务。Runner 在 test 上评估最终冻结的 Snapshot，test 反馈不参与反思和编译。

## 运行小示例

按[安装指南](./guide/installation.md)准备环境。在 [examples/quickstart.yaml](../examples/quickstart.yaml) 中配置两个角色，然后将 Worker 切换为 Agent，从仓库根目录使用其本地 QA 数据运行：

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

这会分两批消费内置的六道 train 题，便于观察后续批次是否复用技能，但不保证触发编译：每批需要可用的失败经验，这里的 general 技能修订还需要至少三个不同任务支持。追加 `--check-only` 可检查初始化，不调用模型，也不验证模型认证或技能使用。

专用的 [Evo-Harness 配置](../examples/evo_harness.yaml)同样采用 MiniAgent 作为两个角色。更换提供商或 harness 时，参见[配置与 CLI](./guide/configuration.md)及 [Harness 接入](./integrations/harnesses.md)。

## 搜索与选择

1. 按 train 提供的顺序单次消费，最多执行 `max_steps` 批。每批全部任务共用 current Snapshot，更新前记录分数。
2. 只反思未完成或分数低于 `success_threshold` 的任务。Worker 读取训练记录证据，返回 `lesson`、`trigger`、`evidence`、`scope_hint`，也可返回 `{"skip": true}`。经验 JSON 或必要字段不合法时记录拒绝。
3. 若存在可用经验，依次通过同一 Worker 的 `edit()` 调用 Topic Curator 和 General Curator，创建、合并、修订或删除原生技能。第二级从第一级已接受的结果开始。
4. 按组件权限、归属、来源、层级和库预算验证每级提交。非法编辑导致整级拒绝；general 被拒绝不会撤销已接受的 topic 编辑。
5. 合法资源变化直接采用，不比较 dev 或在线分数。下一批使用更新后的 Snapshot；无变化编辑不激活新版本。
6. 记录 `online_score` 和 `online_tasks`，返回 current，供最终 test 使用。

`online_score` 是单次、更新前的训练结果均值，衡量沿任务流变化的 Agent，不是最终冻结版本的 test 表现，必须与 `test_score` 分别报告。

## 技能中哪些内容会改变

受管技能使用原生目录，例如 `skills/verify-evidence/SKILL.md`；YAML frontmatter 后是非空正文，可包含代码块：

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

`name` 和 `description` 用于原生发现，`flashrsi` 记录归属、层级和来源任务。每个新增或修订技能都必须引用当前批次的可用经验；general 技能需要 `general_min_tasks` 个不同来源 ID。旧证据不能自动为新修订提供依据，未修改技能则在库和 Snapshot 历史中保留来源。

每个 Curator 只能编辑指定层级。已有非受管技能不能被添加来源后接管，也不能被顺带修改。剥离来源、新文件缺少来源，或用新目录技能遮蔽同名平铺技能，都会被拒绝。检查基于实际 Snapshot 变化，包括 frontmatter 变化；它验证结构和来源 ID，不验证经验的语义真实性或通用性。

MiniAgent 保留配置中的平铺技能加载，并通过 workspace loader 发现目录 `SKILL.md`，提供 `read_skill` 按需读取正文。Claude Code 使用原生 plugin 发现与 `Skill` 工具；显式工具白名单须允许 `Skill`。Curator 无需修改配置或 `CLAUDE.md`，详见 [Agent 接入说明](./agent-integration.md)。

Worker 执行失败向外报错。非法经验可以跳过，非法编译编辑会被拒绝。验证阶段不执行候选技能程序，其行为通过后续原生 Agent 运行检验。

## 参数与成本

以下实现默认值放在 `algorithm` 下。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `batch_size` | `4` | 每批共用一个 Snapshot 的任务数；至少 1。 |
| `max_steps` | `null` | 批次上限；正整数或完整 train 流。 |
| `success_threshold` | `1.0` | 未完成或分数低于此值时反思。 |
| `general_min_tasks` | `3` | 每个新增/修订 general 技能的本批不同来源数；至少 2。 |
| `max_general_skills` | `10` | 受管 general 技能数量上限；至少 1。 |
| `max_topic_skills` | `30` | 受管 topic 技能数量上限；至少 1。 |
| `max_skill_chars` | `2000` | 单个受管技能正文字数上限；至少 1。 |
| `max_library_chars` | `48000` | 受管技能记录（含正文）的规范化 YAML 序列化总字符上限；至少 1。 |
| `max_evidence_chars` | `24000` | 反思和编译证据字符上限；至少 1。 |

若 `batch_size < general_min_tasks`，任何新增或修订 general 技能都无法满足来源要求。即使批次足够大，可用失败经验也可能不足。

每个被消费的 train 任务执行一次；每个失败结果调用一次反思；至少有一条可用经验的批次调用两次目录编辑。这些调用内部的原生 Agent 轮次及最终 test 另计。库预算限制编译产物，不限制每次任务上下文中加载技能的数量或大小。

## 查看实验记录

`declined` 区分 `memory` 解析失败与 `compile` 验证失败。`compile` 决策包含 `level`、`step`、父/候选 ID、`changed_paths` 和 `accepted`。接受表示合法变化被采用，不表示性能提高；无变化编译为 `accepted: false`。

最终 `stream` 决策标识保留的 Snapshot 和在线指标。评估复用时，应查看后续批次或冻结 test 中的原生技能发现与实际加载；仅展示 description 不能证明使用了正文。

## 来源与适配

[实现](../flashrsi/algorithms/evo_harness/__init__.py)将 `A-EVO-Lab/a-evolve` 的 `release/evo-harness` 分支固定到 `3c7c7b8c62a3f07da1d4407aa03f0ca079954bcf`，论文编号为 `2608.15071v2`。[提示模板](../flashrsi/algorithms/evo_harness/_prompts.py)定义反思和编译角色。

本地适配从记录上下文反思，不续接 solver 会话；原生技能选择/加载替代独立 Selector 调用；Curator 编辑原生文件，替代嵌在 prompt 中的 JSON 操作库。最终 Snapshot 可独立运行。历史 prompt 内嵌 JSON 库不符合该文件协议，需要重新演化。当前修订依据本批证据，不继承旧来源计数。这些变化保留流程，但不等同于完整上游实验复现。

## 验证范围

[行为测试](../tests/test_evo_harness.py)和[验证测试](../tests/test_evo_harness_validation.py)覆盖原生文件策展及归属/来源边界。本地历史报告 `docs/smoke-evoharness-20261023.json` 将 MiniAgent × HotpotQA 记为 `flow_passed_with_quality_limits`。三题训练批次覆盖了 topic 与 general 编译；此前全部成功的批次没有触发编译。

报告发现技能保留了训练题特定答案，说明 schema 检查不保证语义泛化。该运行没有后续训练批次证明复用，最终 test 展示了技能 description，但未调用 `read_skill`。独立加载探针不能替代端到端任务使用证据。

这些是本地被忽略报告中的有限观察，不是公开结果链接，也不声称覆盖全部组合。本次文档更新未增加模型调用。应分别报告原生加载、文件变化、来源证据、编译层级和 online/test 指标；编译成功本身不能证明性能收益。
