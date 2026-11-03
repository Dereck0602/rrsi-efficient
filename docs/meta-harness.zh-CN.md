# Meta-Harness

[English](./meta-harness.md) · **简体中文**

[算法总览](./integrations/algorithms.md) · [示例配置](../examples/meta_harness.yaml)

Meta-Harness 让 coding Worker 在构造候选前，从文件检索历史源码、分数和完整轨迹。每次可编辑目录都从初始 Snapshot 开始，Worker 可以复用或组合任何已测版本。外循环保留全部已测候选，结束时只提升 dev 最佳版本，同分保留此前选中的版本。

## 使用条件

dev 必须非空，不使用 train。Dev 反馈用于诊断和选择，因此属于搜索反馈，不是独立最终结果。Runner 在选择结束后评估 test，算法不会把 test 数据加入搜索历史。

Worker 必须是具备文件读取、搜索和编辑工具的 Agent（`algorithm.worker.type: agent`），模型独立于 Target 配置。可编辑全部 Target 已声明组件，但仍受原生组件布局和执行接口约束。`evolve_scope = {"*"}` 不会暴露闭源 harness 的内部实现。

## 运行小示例

按[安装指南](./guide/installation.md)准备环境，在 [examples/quickstart.yaml](../examples/quickstart.yaml) 中配置两个角色。从仓库根目录使用其本地 QA 数据运行：

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm meta_harness \
  --algorithm.worker.type agent \
  --algorithm.worker.name miniagent \
  --algorithm.worker.max_turns 30 \
  --algorithm.max_steps 2
```

两次尝试让第二次 Worker 调用有机会查看此前候选或拒绝记录，但不保证实际检索历史或生成合法编辑。追加 `--check-only` 可检查初始化，不调用模型，也不检查远端认证。参见专用 [Meta-Harness 配置](../examples/meta_harness.yaml)与[配置和 CLI](./guide/configuration.md)。

## 搜索与选择

1. 在完整 dev 上评估初始 Snapshot，将其保存为 seed 和 best，并保存分数与评估记录。
2. 每次尝试都检出 seed 作为可编辑目录，在内部临时导出所有已测 Snapshot 的原生源码，并复制已有框架轨迹和决策。
3. Worker 自行选择文件检索、诊断失败并编辑一个候选。外循环不按分数指定历史父版本，也不安排固定 mutation、crossover 或反思阶段。
4. 提交前移除临时历史和已知运行时产物。非法或无变化编辑被拒绝，消耗一次尝试，不评估候选；拒绝记录在后续历史中可见。
5. 在完整 dev 上评估合法候选，即使降分也保留在历史中。只有严格提升才更新 best。
6. `max_steps` 结束后提升 best；没有候选提高时保留 seed。搜索期间不切换 Target 的活动版本。

候选 `parent_id` 始终是实际 checkout/commit 基线 seed，不表示 Worker 复制了哪个历史机制。这与 [ADAS](./adas.zh-CN.md) 不同：后者每次从当前最佳版本检出，提示携带说明/分数，而非完整历史文件。

## 历史与资源边界

临时历史包含：

| 条目 | 内容 |
| --- | --- |
| `index.json` | Snapshot ID、父 ID 和原生源码 checkout 路径。 |
| `sources/` | 每个已测版本，包括降分版本，保留二进制内容和可执行权限。 |
| `trajectories/` | 已有的脱敏评估、分数、决策和此前 Worker 轨迹。 |

索引中的源码路径相对历史目录，给 Worker 的历史路径相对编辑目录，因此工作目录搬移后引用仍可用。历史是提示要求只读的参考资料，不是操作系统强制的只读挂载：误改只影响临时副本，提交前会移除。下一轮从原始记录和 Snapshot 重建。

历史不拼成一条长提示，也不受算法级证据字符上限截断，但仍受 Worker 自身上下文和工具输出限制。每轮重新导出完整已测历史，磁盘工作量随搜索增长；没有历史缓存或索引数据库。

直接在 Python 中调用 `evolve()` 且没有运行目录时，使用临时记录目录，结束或失败后清理。Runner 调用保留正常实验记录。Worker 失败向外传播，临时参考资料与 checkout 会清理，不作为资源文件采用。该机制支持工作目录搬移，不代表已经验证全部容器后端。

## 参数与成本

算法只定义一个参数，模型和执行预算放在对应角色下。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `max_steps` | `20` | 编辑尝试数；整数，至少 1，非法尝试也计数。 |

设 dev 有 D 题、最多 S 次尝试，Target 搜索最多执行 `(1 + S) * D` 次任务。正常完成时有 S 次 Worker 编辑调用，非法编辑跳过候选 dev 评估。原生模型/工具轮次与最终 test 另计。重复历史设计可能再次测量：除检查是否改变 seed 外，没有全局候选去重。

## 查看实验记录

`kind: decline` 记录未测尝试。`kind: admit` 记录已测候选、实际父版本、分数和 `improved_best`；准入表示加入历史，不是最终选中。最终 `promote` 包含所选分数、基线分数和 `candidate_ids`，列表也包括 seed。

确认历史使用情况应检查 Worker 的文件/工具轨迹。提供历史本身不能证明 Worker 读取过。应将候选资源与它声称参考的版本对照，仅凭 `parent_id` 无法确认思路来源。

## 来源与适配

[实现](../flashrsi/algorithms/meta_harness/__init__.py)将 `stanford-iris-lab/meta-harness` 固定在 commit `8123ccabe2b19fa1123090b2e5f1bacc20963ce8`，参考文本分类和 Terminal-Bench 示例。[提示](../flashrsi/algorithms/meta_harness/_prompts.py)定义文件检索与编辑边界。此前接入记录关联论文编号 `2603.28052`。

本地适配使用一个 seed、每次一个候选，以及一个 Benchmark 主指标，选出一个最终 Snapshot。未实现多个初始 harness、accuracy/token 多目标前沿、跨进程恢复、上游任务专用 import/smoke 检查或集群调度。提示中的泛化要求不等于独立反作弊验证。
