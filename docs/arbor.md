# Arbor 算法接入

来源：[RUC-NLPIR/Arbor](https://github.com/RUC-NLPIR/Arbor/tree/7cdaf1fa6d779b3d5e340052357bf3fe55dfed93)，
核对版本 `7cdaf1fa6d779b3d5e340052357bf3fe55dfed93`。主要参考
`src/coordinator/idea_tree.py`、`prompts.py`、`tools/tree_ops.py`、
`tools/executor_run.py` 与 `tools/git_ops.py`。

这是放在 `flashrsi/algorithms/arbor/` 下的核心机制适配：假设树、Coordinator
选择与剪枝、从当前 trunk 出发的 Executor，以及从实验向祖先逐级归纳经验。
原版独立 CLI、MCP、Dashboard 和 benchmark zoo 不属于本算法接入。

## 搜索流程

1. 对初始 Snapshot 分别评估完整 train、dev；存在失败任务则终止，避免用无效基线搜索。
2. Coordinator 从 `ROOT` 扩展研究方向，再扩展具体实验假设。它可以选择待执行叶节点、
   继续扩展已有方向或已测实验、剪掉整个子树，或提前停止。
3. Executor 在当前 trunk 的 checkout 中实现选中的假设。树上的 `parent_id`
   表示概念关系，`base_snapshot_id` 表示这次编辑实际使用的代码基线，两者各自保留。
4. 合法且有实际改动的候选在 train 上评估。候选相对当前 trunk 的 train 增益达到
   `merge_threshold`，才进入 dev 检查。dev 严格提高才更新内部 trunk；同分保留此前版本。
5. Worker 根据实际实现、train 轨迹和记录的分数提炼实验经验，然后逐层综合兄弟节点的
   经验，更新研究方向与 ROOT 的 insight。退步实验和拒绝原因也保留。
6. 搜索结束时通过 `_promote` 选定最终 Snapshot。runner 再执行 sealed test，
   test 的题目、答案、轨迹和分数不进入 Coordinator、Executor 或 insight 提示。

整个过程使用原有 `Algorithm`、`Worker.ask/edit`、`Snapshot`、`Benchmark` 接口，
没有另建执行框架或按具体 harness / benchmark 名称分支。

## 配置与运行

见 [examples/arbor.yaml](../examples/arbor.yaml)。它从固定 HF 版本抽取 9 道 HotpotQA
context 题目，Target 为 MiniAgent、Worker 为 Claude Code；两处模型及凭据独立配置。
也可以将 Worker 改成其他支持目录编辑的 Agent。

```bash
uv run flashrsi run --config examples/arbor.yaml --check-only
uv run flashrsi run --config examples/arbor.yaml
```

自动实验入口同样可以使用此配置：

```bash
python scripts/run_experiment.py --config examples/arbor.yaml
```

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `max_cycles` | 10 | 最多派发多少次实验；非法编辑或无改动也消耗一次 |
| `max_depth` | 3 | 假设树最大深度，ROOT 为 0，方向为 1，实验从 2 开始 |
| `merge_threshold` | 0.0 | 进入 dev 检查所需的 train 最小增益，使用主指标本身的单位 |
| `max_evidence_chars` | 30000 | 每段 JSON 提示证据的字符上限，超出后明确标记截断 |

每次派发前最多允许 12 次 Coordinator 动作，防止只扩展、剪枝或反复返回非法计划而
无限调用 Worker；超过后明确报错。每次扩展允许 1–3 个子假设。
Worker 调用失败直接中止；非法编辑记录为 `needs_retry`，当前不恢复旧 checkout 或自动重试。
Coordinator 可提出新的替代假设。`done` 只表示得到实验结果，不代表候选被采纳。

设 train/dev/test 题量分别为 N/M/K、实验预算为 C，目标任务执行次数最多为
`(C + 1) * (N + M) + K`；Worker 的规划、编辑和经验综合调用另计。
每轮不会重新评估未变的 trunk。算法要求 train、dev 非空，三个集合的 task ID 唯一且互不重叠。

## 与原版的边界

- 原版使用 B_dev 做 Executor 迭代，合并时可访问 B_test。这里分别映射为 train 和 dev，
  正式 test 保留到最终选定版本后；同分不更新符合 FlashRSI 的既有协议。
- 原版 worktree / Git 合并映射为 Snapshot checkout / 提升，保持“新实验从当前 trunk 出发”。
  不实现真实分支合并冲突处理、恢复 Executor、并行派发或论文完整运行协议。
- Coordinator 用序列化的树、资源路径和有界轨迹反馈规划；Executor 可直接读取编辑目录。
  完整框架轨迹仍落盘，但本版没有原版任意工具调度、文献搜索、跨实验持久经验或交互审批。
- tree 记录属于算法私有状态，不进入目标 Snapshot。每轮 `arbor-tree` 和最后的 promote
  记录包含节点、分数、版本引用和 insight；Worker 轨迹由基类自动记录和脱敏。
