# Arbor

[English](./arbor.md) · **简体中文**

[算法总览](./integrations/algorithms.md) · [示例配置](../examples/arbor.yaml)

Arbor 用假设树组织搜索。Coordinator 扩展研究方向、选择实验，Executor 从当前已选 trunk 实现假设。train 筛选候选，dev 严格提升才允许采纳；实验经验沿树向上综合，供后续规划使用。树的父子关系表示思路，不表示代码继承。

## 使用条件

train、dev 必须非空，train/dev/test 各自的任务 ID 唯一且互不重合。基线 train 和 dev 的所有轨迹都必须完成，否则停止搜索。算法使用 Benchmark 主指标均值，不额外要求 `[0, 1]` 范围，按更高分更好处理。

Worker 必须是独立的文件编辑 Agent（`algorithm.worker.type: agent`），模型和连接独立配置。Coordinator、Executor、反思和综合是发给同一个 Worker 的不同提示角色，不是分别配置的模型。可编辑 Target 声明的全部组件（`evolve_scope = {"*"}`）；编辑运行时代码要求 harness 暴露这些代码。

dev 反复参与选择，不是独立最终测试。test 任务、分数和轨迹不进入这些搜索提示；Runner 在搜索结束后评估选中的 Snapshot。

## 运行小示例

按[安装指南](./guide/installation.md)准备环境，在 [examples/quickstart.yaml](../examples/quickstart.yaml) 中配置两个角色的连接，再从仓库根目录运行：

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm arbor \
  --algorithm.worker.type agent \
  --algorithm.worker.name miniagent \
  --algorithm.worker.max_turns 30 \
  --algorithm.max_cycles 2 \
  --algorithm.max_depth 2 \
  --algorithm.merge_threshold 0.0
```

这会使用本地 SearchQA 数据，最多派发两次实验。Coordinator 可提前停止，每次请求也可能包含多个原生模型/工具调用。追加 `--check-only` 可检查初始化，不调用模型，也不验证远端认证。专用 [Arbor 示例](../examples/arbor.yaml)则使用固定版本的 HotpotQA context 数据，Target 为 MiniAgent，Worker 为 Claude Code。

## 搜索与选择

1. 初始 Snapshot 在完整 train、dev 上各评估一次。创建保存初始分数和版本的 `ROOT`，并将该 Snapshot 设为当前 trunk。
2. Coordinator 选择扩展方向、派发待执行实验叶节点、剪掉子树或停止。它读取假设树、当前 trunk 的文件名与组件规则、当前 train 轨迹以及保存的 train/dev 分数。
3. Executor 在**当前 trunk** 的 checkout 中实现选中的假设。概念父节点可能是被拒实验或另一条研究思路，不会自动检出该父节点的 Snapshot。实际代码基线记为 `base_snapshot_id`。
4. 清理运行时产物并提交。非法资源变化或空编辑记为 `needs_retry`，不评估候选。本算法在 train 评估前没有独立的框架编译/diff review 门禁。
5. 在完整 train 上评估有改动的候选。任一轨迹未完成则不能采纳；否则将均分与当前 trunk 保存的 train 均分比较，增益达到 `merge_threshold` 才评估完整 dev。
6. 所有 dev 轨迹完成且均分严格超过 trunk 保存的 dev 分数时，才采纳候选，并同时更新内部 trunk 及保存的 train/dev 证据。train 未过门槛或 dev 同分、退步都保留 trunk。
7. 提炼本次实验经验，包括失败编辑和退步实验；从下向上逐级读取祖先的有 insight 子节点并综合经验。保存假设树和当前 trunk，再规划下一次实验。
8. 达到实验预算或 Coordinator 停止后，提升最后选中的 trunk。不另做全档案排序，也不重测全部候选。没有任何采纳时保留 seed。

采纳规则为：

```text
all candidate train trajectories finished
and candidate_train - trunk_train >= merge_threshold
and all candidate dev trajectories finished
and candidate_dev > trunk_dev
```

`merge_threshold` 是以主指标为单位的 train 绝对分差，不是相对百分比，也不是 dev 阈值。默认零时，train 同分可进入 dev，但 dev 同分不会替换 trunk。比较没有额外数值容差。未变的 trunk 不会每轮重测，参考值是其保存的测量。

## 树动作与状态

Coordinator 在 `<ARBOR>...</ARBOR>` 中返回一个 JSON 对象，支持以下动作：

| 动作 | 条件与作用 |
| --- | --- |
| `expand` | 在未达 `max_depth` 的 `root`、`direction`、`done` 或 `merged` 节点下新增 1–3 个非空假设。 |
| `run` | 派发深度至少为 2 的 `pending` 叶节点。 |
| `prune` | 将非 ROOT 节点及其全部后代标为 `pruned`，保留记录与经验。 |
| `stop` | 停止搜索，保留当前 trunk。 |

ROOT 深度为 0，方向为 1，可执行假设从 2 开始；ID 按创建顺序生成，如 `1`、`1.1`。单次成功扩展不消耗实验 cycle。每次派发/停止决策最多调用 Coordinator 12 次，扩展、剪枝和非法动作都计数；耗尽后报错，不会静默派发实验。

| 状态 | 含义 |
| --- | --- |
| `root` / `direction` | 规划节点，不是可执行假设。 |
| `pending` | 尚未派发的实验。 |
| `running` | 正在实现实验。 |
| `done` | 已评分但未采纳的实验，包括 train 未过筛选或 dev 拒绝的候选。 |
| `merged` | 候选在当时被采纳，后续实验仍可替换它。 |
| `needs_retry` | 空/非法编辑，或候选评估存在未完成轨迹；没有自动重试。 |
| `pruned` | 不再具备后续规划/执行资格，证据仍保留。 |

当前不能重新派发或扩展 `needs_retry` 节点，因为这些动作要求其他状态。Coordinator 可在其他位置提出替代假设。剪枝不会回滚已采纳的 trunk 或删除其 Snapshot；概念剪枝与资源版本选择是两件事。

## 证据与经验向上传播

Executor 读取所选实验、序列化假设树和祖先经验、组件规则，以及实际 trunk 文件。提示要求实现可复用机制，保持模型、凭据和评估数据不变。它返回文字说明，但候选由实际文件改动定义。

反思读取实验记录和可用的候选 train 轨迹。记录中可能包含已测 dev 分数，但不向反思传递原始 dev 轨迹。dev 分数为 null 表示未评估该阶段，不代表零分。非法编辑只提供空轨迹列表和拒绝原因。

深度为 d 的实验会对它的 d 层祖先逐层调用 Worker 综合，直到 ROOT。每次读取该祖先所有 insight 非空的直接子节点，包括保留的被拒或已剪枝实验经验。更新的是文字经验，不是数值分数或选择概率。实现没有 UCB 公式或数值式树回传规则；Coordinator 通过提示做规划选择。

每个序列化证据块受 `max_evidence_chars` 限制，截断会标记。完整假设树/评估记录仍会落盘，但不能保证每次提示都向 Worker 展示全部历史行。反思与综合提示要求少于 200 词，算法不强制检查这一字数上限。ROOT 保存的 Snapshot/分数描述初始版本；单独的 current-trunk 记录提供最新选择状态。

## 参数与成本

默认值放在 `algorithm` 下。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `max_cycles` | `10` | 最多派发的实验数；整数，至少 1。非法或空编辑也占一次。 |
| `max_depth` | `3` | 假设最大深度；整数，至少 2，ROOT 为 0。 |
| `merge_threshold` | `0.0` | 进入 dev 评估所需的最小 train 增益，非负。 |
| `max_evidence_chars` | `30000` | 每个序列化提示证据块的字符上限；整数，至少 1。 |

设 train/dev/test 分别有 N/M/K 题，C = `max_cycles`，Target 任务执行数最多为 `(C + 1) * (N + M) + K`：一次基线 train/dev、每个实验至多一次候选 train/dev，最后执行 test。非法编辑跳过候选两次评估，train 执行失败或增益不足跳过 dev。这里计的是任务执行，不是 API 请求或原生工具轮次。

每次派发实验会调用 Worker 编辑一次、反思一次，并进行 d 次祖先综合；派发前最多另有 12 次规划调用。停止决策也消耗规划调用，因此即使实验预算不变，树深度也会影响 Worker 成本。

Worker 失败与评估异常向外传播。返回的候选轨迹若存在未完成状态，则记为 `needs_retry`，不自动终止后续规划。没有框架级旧 checkout 恢复、自动实验重试或隐藏修复预算。

## 记录与适配

`arbor-plan` 保存扩展/剪枝及假设树，`arbor-plan-rejected` 保存非法动作反馈，`arbor-stop` 记录提前停止。每次实验和经验传播后，`arbor-tree` 记录 cycle、`trunk_id` 和全部节点。最终 `promote` 保存选中 Snapshot、dev 分数与假设树。Worker 轨迹由公共框架记录。

节点的 `parent_id` 是假设父节点，`base_snapshot_id` 是实际编辑的资源基线，`snapshot_id` 是产出的候选。这些字段与 Snapshot 的资源版本链不同。假设树属于算法元数据，不写入被演化的 harness 资源。

[实现](../flashrsi/algorithms/arbor/__init__.py)固定引用 [RUC-NLPIR/Arbor](https://github.com/RUC-NLPIR/Arbor/tree/7cdaf1fa6d779b3d5e340052357bf3fe55dfed93) commit `7cdaf1fa6d779b3d5e340052357bf3fe55dfed93`，参考 Coordinator 假设树/提示及 executor/tree/Git 工具。

- 上游 Executor 迭代与合并评估信号 `B_dev`、`B_test` 在这里映射为 train、dev。FlashRSI test 保留到最终选择后，本地采纳要求 dev 严格提升。
- Snapshot checkout 和版本选择替代 worktree 与 Git 合并。本地 `merged` 表示采纳，不代表三方 Git merge 或自动冲突处理。
- 实验从当前 trunk 顺序执行。未实现 Executor 续跑、并行派发、跨运行持久经验、任意 Coordinator 工具、文献搜索或交互审批。
- 上游独立 CLI、MCP 服务、Dashboard 和 benchmark 集合不属于本算法移植。执行、评分、资源和记录由本地框架负责，算法不按具体 harness 或 benchmark 写分支。
