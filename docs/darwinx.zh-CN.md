# DarwinX

[English](./darwinx.md) · **简体中文**

[算法总览](./integrations/algorithms.md) · [示例配置](../examples/darwinx.yaml)

DarwinX 按“保留已有能力并扩展能力”的契约搜索 harness 版本。候选通过低成本筛选和重新执行的全量 train 确认后，才有资格成为普通变异的父版本。已经完成全量评估但被拒绝的分支，仍可通过重组贡献互补能力。最终在已准入版本（含初始版本）中按保存的 train 分数选优，不使用 dev。

## 使用条件

train 必须非空，且 `task_id` 唯一。每次评估返回的逐题主指标必须在 `[0, 1]`，算法会在收到结果时检查。本算法不要求 dev。train 同时用于搜索证据、准入和最终选优，其分数不是独立的泛化证据。Runner 在搜索结束后对选中的 Snapshot 进行 test 评估。

Worker 必须是文件编辑 Agent（`algorithm.worker.type: agent`），模型与连接独立配置。`evolve_scope = {"*"}` 允许编辑 Target 声明的全部组件。修改运行时代码要求 Target 暴露这些代码，不会因此开放闭源 harness 内部实现。详见 [Harness 接入](./integrations/harnesses.md)。

## 运行小示例

按[安装指南](./guide/installation.md)准备环境，在 [examples/quickstart.yaml](../examples/quickstart.yaml) 中配置 Target 和 Worker 的连接，再从仓库根目录运行：

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm darwinx \
  --algorithm.worker.type agent \
  --algorithm.worker.name miniagent \
  --algorithm.worker.max_turns 30 \
  --algorithm.max_steps 1 \
  --algorithm.screen_size 2 \
  --algorithm.screen_samples 1 \
  --algorithm.confirm_samples 2 \
  --algorithm.merge_every 0
```

这会请求一次变异尝试，确认阶段重复两次，不进行重组。只有候选通过筛选才会进入确认。追加 `--check-only` 可检查初始化，不调用模型，也不验证远端认证。专用 [DarwinX 配置](../examples/darwinx.yaml)启用周期重组。覆盖方式见[配置与 CLI](./guide/configuration.md)。

## 搜索与选择

1. 在完整 train 上评估初始版本 `confirm_samples` 次，保存总均分和逐题均分。达到 `solved_threshold` 的任务构成初始保留集合；初始版本始终是可选父版本，也是最终选择的回退版本。
2. 从已准入版本中选择变异父版本：以 `exploration_probability` 的概率均匀抽样，否则按 `(cumulative_gain, score)` 最大者选择。两个排序键都相同时保留较早入档者。累计收益沿主父版本链累积，与最终按总分排序不同。
3. 每到第 `merge_every` 次尝试，优先尝试重组。两个分支都必须到达全量确认，且各自至少解决一道对方未解决的任务；这里不要求准入。未尝试过的候选对先按已解决任务并集大小、再按双方较低分排序，仍相同则按档案枚举顺序选择。较高分分支作为主版本，同分时较早分支为主版本。
4. Worker 获得主版本 checkout、父版本记录、保留集合、主版本轨迹、近期档案、经验、被拒编辑与组件规则。重组时，工作目录内还提供临时共同祖先和 donor checkout。提示要求只读使用参考目录；这些目录在提交候选前移除。
5. 清理运行时产物并提交。非法资源变化、空编辑、与任意已归档 harness 重复的文件状态都会被拒绝，不评估、不提炼经验，但仍消耗一次尝试。选中的重组对即使编辑被拒也算尝试过；没有未尝试的互补对时退回普通变异。
6. 在随机 train 子集上筛选候选，所有父版本在相同任务、相同重复次数下重新评估。筛选通过后，所有父版本和候选再重新评估完整 train，执行下节的准入检查。
7. 每个已评估候选都进入档案，包括筛选失败和确认失败者。Worker 通过 `<LEARNINGS>` 总结该次实验。只有通过确认的候选进入变异父版本池，并将新解决任务加入继承的保留集合。
8. 在已准入版本中选择保存的 train 均分最高者。完全同分时保留较早入档者。最终选择不会重测或刷新旧档案分数；即使候选胜过父版本本次确认的重测结果，也可能最终保留初始版本。

Target 活动版本只在最终提升时切换。`donor_id` 表示重组输入；Snapshot 的父子关系只沿主父版本建立，共同祖先查找也只沿这些主父版本链接进行。

## 筛选与确认门禁

每次重复执行完整的待测任务批次。逐题分数是多次执行均值（**avg@k**），不是 best-of-k 或 pass@k。令 `c[t]` 为候选逐题均分，`p[t]` 为主父版本本次重测均分，`r[t]` 为所有父版本本次重测逐题均分的最大值：

```text
delta[t] = c[t] - p[t]
gain = sum(delta[t])
regression_mass = sum(max(r[t] - c[t], 0))

screen passes:
    any delta[t] > 1e-12
    and regression_mass <= screen_regression_tolerance + 1e-12

confirmation passes:
    gain > 1e-12
    and regression_mass <= regression_tolerance + 1e-12
    and every inherited invariant task has c[t] >= solved_threshold
```

普通变异中 `r[t] = p[t]`。重组的回退参考取双方逐题最大值，但 gain 仍相对主版本计算。重组保留集合为双方继承的 invariant 与双方已解决任务的并集。因此整合双方已有能力就可能扩展主版本，不要求额外解决并集之外的新任务。

回退质量是**逐题下降量的和**，不是平均下降或净变化。默认确认容差为零，因此不能用其他题的提升抵消超过数值容差的下降。即使回退容差非零，也必须满足 invariant 保留要求。筛选不执行完整的 invariant 门禁；只要有局部提升且回退在容差内，即使筛选总净收益不为正，也可能进入确认。

`solved_threshold: 1` 表示每次已观测重复都得 1 分才计为已解决。已准入版本沿版本链累积这些任务。这是有限样本检查，没有置信区间、显著性检验或对未见任务的保证。父版本的新测量不会覆盖其旧档案分数或已解决集合。

## 参数与成本

实现默认值放在 `algorithm` 下。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `max_steps` | `10` | 编辑尝试数；整数，至少 1。变异、重组和被拒编辑均计数。 |
| `screen_size` | `3` | 筛选任务数；整数，至少 1，最多取 train 大小。 |
| `screen_samples` | `1` | 每个待筛选版本的重复次数；整数，至少 1。 |
| `confirm_samples` | `3` | 初始版本与各确认版本的重复次数；整数，至少为 `screen_samples`。 |
| `screen_regression_tolerance` | `1.0` | 筛选允许的回退质量，非负。 |
| `regression_tolerance` | `0.0` | 确认允许的回退质量，非负。 |
| `solved_threshold` | `1.0` | 已解决任务的逐题均分阈值，大于 0 且不超过 1。 |
| `exploration_probability` | `0.5` | 均匀抽取变异父版本的概率，范围 `[0, 1]`。 |
| `merge_every` | `3` | 每多少次尝试优先重组；整数，至少 0，0 表示禁用。 |
| `seed` | `42` | 父版本与筛选任务采样的整数随机种子，不控制模型采样。 |
| `max_evidence_chars` | `24000` | 每个序列化证据块的字符上限；整数，至少 1。档案、经验和拒绝记录按最新优先展示。 |

设 train 有 N 题，s = `min(screen_size, N)`，q = `screen_samples`，k = `confirm_samples`：

| 阶段 | Target 任务执行数 |
| --- | --- |
| 初始版本 | `N * k` |
| 变异筛选 | `2 * s * q` |
| 变异确认（若进入） | 额外 `2 * N * k` |
| 重组筛选 | `3 * s * q` |
| 重组确认（若进入） | 额外 `3 * N * k` |

每次尝试调用一次 `worker.edit`。每个已评估候选还会调用一次 `worker.ask` 提炼经验，即使该候选被拒。未评估的非法编辑跳过候选评估与经验提炼。以上不含最终 test、原生模型/工具轮次和后端重试；统计的是任务执行数，不是 API 请求数。Worker 失败和评估异常向外传播，没有算法级修复循环。

## 查看实验记录

评估记录保留原始重复执行。准入决策包含 `per_task`、`samples`、`fidelity`、`accepted`、`gain`、`regression_mass`、`lost_invariant`、`delta` 和 `parent_per_task`，最后一项保存此次比较使用的新测量。`fidelity: confirmation` 仅表示执行过完整确认，**不代表准入成功**。

`kind: admit` 表示已评估版本入档，其中也包含被拒版本；`kind: decline` 表示未评估的编辑。`learnings` 保存 Worker 总结，最终 `promote` 保存选中 Snapshot、档案、经验与被拒编辑。仅筛选过的版本分数只覆盖采样任务，不能当作全量分数直接排名。

## 来源与适配

[实现](../flashrsi/algorithms/darwinx/__init__.py)固定引用 [SalesforceAIResearch/Beagle](https://github.com/SalesforceAIResearch/Beagle/tree/165343b46a85b5d28c6370e6197ac665c7eda871/beagle/algorithms/darwinx) revision `165343b46a85b5d28c6370e6197ac665c7eda871`，主要参考 `vendor/evolve/preserve_extend.py`、`parent_selection.py`、`merge_selection.py` 和 `pipeline.py`。这是机制移植，不是上游完整实验流程的复现。

- 用 FlashRSI Snapshot/Worker/Benchmark 接口替代 Git worktree、SQLite 状态和 campaign 管理，不支持跨进程续跑。
- 使用固定 train 池、数值门禁和版本链 invariant，未移植上游更广泛的可配置裁决与任务选择机制。
- 本地探索对合格父版本均匀采样；上游 `mixed_high_score` 的 broaden 分支仍按累计收益和分数排序，再用子节点数等打破平局。本地重组对按已解决任务并集大小和分数选择，不采用上游 utility/risk 门禁。
- 每次变异只调用一次编辑，没有内部 analyze/implement/review 与修复循环。重组只支持两分支，不支持 N-way merge 或 prune/consolidate。
- 未实现教师模型、独立 verifier/anti-cheat、跨 benchmark 或 held-out 准入门禁、轨迹质量控制、集群调度。提示中的约束不等于独立反作弊检查。
- 提示证据受字符预算限制，重组源码通过 checkout 读取。默认算法按 train 最终选优；单独授权实验的 dev 选优协议不会改变此默认实现。
