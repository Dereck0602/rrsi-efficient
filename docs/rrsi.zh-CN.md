# RRSI

[English](./rrsi.md) · **简体中文**

[算法总览](./integrations/algorithms.md) · [示例配置](../examples/rrsi.yaml)

RRSI 根据轨迹调查和编辑历史，在逐渐收缩的机制预算内提出 harness 修改。候选必须满足历史分数底线以及分数/token 成本规则。每轮选出分数最高的可接受候选，再从胜者继续搜索。最终返回最后接受的版本，不一定是历史最高分版本。

## 使用条件与小示例

train 必须非空、任务 ID 唯一，主指标分数在 `[0, 1]`。Worker 必须是独立的文件编辑 Agent（`algorithm.worker.type: agent`），Target 与 Worker 分别配置模型与连接。可编辑范围为 Target 声明的全部组件；RRSI 语义标签不额外授予文件权限。不使用 dev，Runner 在最终选择后评估 test。

按[安装指南](./guide/installation.md)准备环境，在 [examples/rrsi.yaml](../examples/rrsi.yaml) 中配置两个角色，再从仓库根目录使用本地任务运行：

```bash
flashrsi run --config examples/rrsi.yaml \
  --benchmark.data_path ./examples/data/quickstart.jsonl \
  --benchmark.limit null \
  --benchmark.split_counts '{train: 3, dev: 1, test: 1}' \
  --algorithm.T 1 \
  --algorithm.m 1 \
  --algorithm.k 2 \
  --algorithm.m_draft 1 \
  --algorithm.n_fail_traces 2 \
  --algorithm.n_success_traces 1
```

这会请求一轮、一个候选，每次全量测量重复两次。调查、审查、修复和原生 Agent 轮次仍可能产生很多 Worker 调用。追加 `--check-only` 可检查初始化，不调用模型，也不验证远端认证。实际搜索需要完整的 Target token 用量；初始化不能证明这些统计可用。

## 搜索与调查

1. 对初始 Snapshot 的全部 train 任务执行 `k` 次，保留逐题分数、轨迹、token 成本及可选权重和 guard 指标。`delta` 为 null 时根据基线 trial 校准噪声带，否则直接使用配置值。
2. 按任务实测均分排序，取最多 `n_fail_traces` 个低分任务的最差 trial，再取最多 `n_success_traces` 个未入选高分任务的最佳 trial。这些标签表示排序位置，不是硬性的成功/失败阈值。
3. analyst 读取任务表，调度 digester 调查代表轨迹。digester 通过 Worker 工具读取独立的脱敏轨迹文件，支持失败、能力、成功三种视角及追问；切换视角不会提供另一条 trial。至少完成一次 digest 才能形成报告。
4. 固定本轮 context，包含分析、代表轨迹、当前分数/成本、历史最佳分数、编辑预算、探索/剪枝建议及既有编辑历史。`m` 个兄弟候选均从同一当前 Snapshot 出发、读取同一 context，不在本轮内借用其他兄弟候选的结果。
5. 依次生成候选，检查与审查改动，再编译并执行少量原生 smoke。全部提案完成后，对合格候选做完整测量并按下节规则选择。比较使用保存的 incumbent 测量，不会每轮重新测 incumbent。
6. 选出本轮胜者后，记录每项编辑的结果与归因。有胜者则推进搜索版本，否则保留 incumbent。历史最高分只根据已接受 incumbent 更新。完成 `T` 轮后，统一提升最终当前版本。

## 准入、同分与最终选择

设 S、C 为 incumbent 的分数与每次 Target 任务的平均 token 成本，S'、C' 为候选值，S* 为已接受版本达到的最高分：

```text
dS = S' - S
dC = (C' - C) / C

if S' < S* - delta:
    reject
elif dS > delta:
    require dC <= beta0 + beta1 * dS
else:
    require w_s * dS - w_c * dC + w_n * novelty > 0

also require all configured guards to pass
```

novelty 统计此前获胜编辑中未出现过的不同 `client_tool`、`skill`、`memory`、`subagent` 标签数量，其他标签不获得此奖励。标签按每项声明的实际 diff 核对，可用 `component_signals` 补充正则证据。

可接受候选之间按**分数**选择，不按效用最大或成本最低选择。候选同分时保留较早生成者。incumbent 不参与这个同分比较：同分候选可以凭较低成本或新颖性替换它；稍低分候选也可在历史底线内因效用规则通过而替换它。没有合格候选则保留 incumbent。

`guards` 按相对 incumbent 的 Benchmark 指标非加权均值变化执行 `max_drop` 和/或 `max_rise` 约束，与分数/成本规则共同生效。可接受但输给其他候选的版本记为 `LOST`；只有胜者进入已接受组件的新颖性历史。最终返回最后接受的版本，不恢复 S* 对应的 Snapshot。

## 分数、失败、成本与校准

默认对全部 train trial 等权求均值；也可用 `weight_metric` 指定每个 trial 都必须提供的严格正权重。状态为 error 的 trial 奖励置零，仍保留在分母中；其比例超过 `invalid_missing_frac` 则测量无效。权重、guard 指标或 token 用量缺失，以及平均 token 成本非正，也会使测量无效。基线无效则中止，候选测量无效则拒绝该候选；框架协议错误和 Worker 失败仍可能向外传播。

成本为每次完整 Target 任务执行的 token 总数均值，包括缓存 token 和已报告的内部辅助模型/子 Agent 用量，不包括演化 Worker 调用。读取器支持原生 usage 格式，优先采用非空 SDK `model_usage` 会话汇总，避免只算父 Agent 或重复计算总量，不能推断未报告用量。这是 token 成本，不是金额或耗时。

`delta: null` 时默认在每个任务内部对 trial 做 2,000 次 bootstrap，使用 `delta_z * sqrt(2) * std(bootstrap scores)`。多次基线测量有正的样本标准差时据此计算噪声带，否则对 pooled trial 做任务内 bootstrap 并缩放回原测量样本量。校准保留第一次基线作为 incumbent，不挑最好的一次。自动校准要求 `k >= 2` 或 `calibration_evals >= 2`。

Runner 的最终 test 分数仍遵循框架主指标算术均值契约，没有额外的 RRSI 加权 test 分数。加权搜索分数与最终均值可能代表不同目标。

## 编辑预算、审查与修复

从零计数的第 t 轮，其独立机制预算为：

```text
ceil(b_min + (b_max - b_min) * (1 + cos(pi * t / T)) / 2)
```

实现先将中间值保留九位小数再向上取整。实际轮次为 `t = 0 ... T-1`，最后一轮不保证恰好达到 `b_min`。一个机制可以涉及多个文件，预算不是文件数。过去 `w` 轮分数增益不超过 `delta` 时，最后 `m_draft` 个槽要求实际探索尚未尝试的语义组件（若仍有）。prune 建议针对最近 `n_prune` 窗口没有正向实测收益的组件；这是需要审查的删除提案建议，不自动删除。

`<EDITS>` 内用 JSON 声明独立机制，包含 `id`、`component`、`hypothesis`、`targets_mode`、`predicted_affected`、`retroactive_check`、`paths`；可选 `operation` 为 `change` 或 `prune`。ID 必须唯一，路径必须覆盖真实累计 diff。支持的标签是 `prompt`、`control_flow`、`config`、`output_plumbing`、`context_mgmt`、`client_tool`、`skill`、`memory`、`subagent`。

每个候选先编辑一次，最多再做 `repair_rounds` 次同目录修复。Worker 读取 `context.json` 和 `current-diff.txt`，修复后的声明要覆盖相对 incumbent 的全部变化。smoke 前处理 Snapshot 约束、空编辑、凭据模式检查、二进制 diff 拒绝、critic 意见与探索槽要求。critic 审查完整 diff，返回 `<REVIEW>` 裁决；无法解析时最多调用三次，然后按拒绝处理。

以上门禁通过后，编译候选中的全部 `.py` 文件，再按 train 顺序执行最多 `smoke_n` 道已选代表任务。error 状态使 smoke 失败，这里不要求分数提升。`smoke_n: 0` 只编译、不执行。编译或 smoke 失败直接返回无效候选，不重新进入该候选的修复循环。临时证据文件在资源提交前移除。

digest 引用必须匹配某个解码后轨迹值的原文子串，轨迹文件必须保持不变。每份 digest 上限 6,000 字符、最多三次协议尝试；analyst 交互最多 30 轮。这些是协议常量，不是可配置搜索预算。

## 参数与成本

默认值放在 `algorithm` 下，RRSI 会拒绝未知算法参数。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `T` | `20` | 搜索轮数，整数，至少 1。 |
| `k` | `2` | 每次全量 train 测量的重复数，整数，至少 1。 |
| `m` | `2` | 每轮兄弟候选槽数，整数，至少 1。 |
| `b_min` | `1` | 机制预算下限参数，整数，至少 1。 |
| `b_max` | `4` | 初始机制预算参数，整数，不小于 `b_min`。 |
| `w` | `3` | 停滞回看轮数，整数，至少 1。 |
| `m_draft` | `1` | 停滞时保留的探索槽数，整数，范围 `[0, m]`。 |
| `delta` | `None` | 非负分数噪声带，null 表示自动校准。 |
| `delta_z` | `2.0` | 非负校准倍数。 |
| `beta0` | `0.1` | 明确涨分时允许的基础相对成本增幅，非负。 |
| `beta1` | `40.0` | 每单位明确分数提升允许的额外成本增幅，非负。 |
| `w_s` | `100.0` | 噪声带内的分数权重，非负。 |
| `w_c` | `15.0` | 噪声带内的相对成本惩罚权重，非负。 |
| `w_n` | `0.5` | 结构新颖性权重，非负。 |
| `n_prune` | `4` | 剪枝建议的近期轮数窗口，整数，至少 1。 |
| `repair_rounds` | `5` | 额外提案修复次数，整数，至少 0。 |
| `invalid_missing_frac` | `0.15` | 允许的 error trial 比例上限，范围 `[0, 1]`。 |
| `seed` | `7` | bootstrap 整数随机种子，不控制模型采样。 |
| `n_fail_traces` | `22` | 低分代表任务数，整数，至少 0。 |
| `n_success_traces` | `6` | 高分代表任务数，整数，至少 0；两类请求总数必须为正。 |
| `calibration_evals` | `1` | 自动校准时基线测量总次数，整数，至少 1。 |
| `smoke_n` | `2` | 每个候选 smoke 的代表任务数上限，整数，至少 0。 |
| `component_signals` | `{}` | 语义标签到非空正则列表的映射，不是文件权限。 |
| `weight_metric` | `None` | 可选的逐 trial 正权重指标。 |
| `guards` | `{}` | 指标名到非负 `max_drop`/`max_rise` 限制的映射。 |

设 train 有 N 题，每次完整测量执行 `N * k` 次 Target 任务。自动 delta 时基线/校准成本为 `N * k * calibration_evals`，显式 delta 时为 `N * k`。完整候选测量最多 `T * m * N * k` 次，候选 smoke 最多额外 `T * m * min(smoke_n, N, n_fail_traces + n_success_traces)` 次。无效提案会减少这些次数；最终 test、重试和原生模型/工具轮次另计。

Worker 成本独立计算：每个候选最多编辑 `repair_rounds + 1` 次，每次到达 critic review 时最多调用三次。每轮还有 analyst 和 digest 调用，每批 digest 请求最多调查八条轨迹。机制预算不限制这些调用的总 API 请求数。

## 记录与适配

`measurement` 记录分数、成本、trial 证据和 guard 指标，`calibration` 记录噪声带，`rrsi-context` 固定共同的轮次证据。`critic`、`repair`、`smoke-gate` 解释提案处理。`edit-history` 保留完整 diff、`admissible`、`accepted`、`ACCEPTED/LOST/REJECTED`、分数/成本变化及归因。最终 `promote` 保存最终分数/成本、历史最高分和 delta。

归因根据逐题均分变化核对预测任务，并标记至少下降 `1/k` 的未预测退步。未知预测任务 ID 不进入命中率分母。捆绑编辑共享一次候选测量，归因不是每项机制的因果消融。无效或未测候选保留原因，但不计入已测组件尝试与收益统计。

[实现](../flashrsi/algorithms/rrsi/__init__.py)独立移植 [google-research/rrsi](https://github.com/google-research/rrsi/tree/be50316e1db05914068a973f322770ef08ed7ba1) 的机制，固定版本为 `be50316e1db05914068a973f322770ef08ed7ba1`。执行、评分、编辑、版本管理和记录分别复用 Agent、Benchmark、Worker、Snapshot、Algorithm。未复制上游 Git 管理、Domain adapter、角色专用模型配置、恢复/重评命令与起始 harness。SearchQA/MiniAgent 示例是本地适配，不是原始 coding 实验配置。
