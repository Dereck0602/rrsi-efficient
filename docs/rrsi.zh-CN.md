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
3. analyst 读取任务表，调度 digester 调查代表轨迹。digester 通过 Worker 工具读取独立的脱敏轨迹文件，支持失败、能力、成功三种视角及追问；切换视角不会提供另一条 trial。`digest_concurrency` 控制同一批独立 digest 的并发上限（默认 1），结果按请求顺序汇总；analyst 的后续决策等本批完成后再执行。至少完成一次 digest 才能形成报告。
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

完整测量将 `N * k` 次任务执行放入同一个队列，共享 Target 的
`concurrency` 上限；槽位覆盖环境准备、Agent 执行、verifier 和清理全过程。
默认 `task_order: fifo` 按 repeat/任务顺序入队；第一遍仍有慢任务运行时，
空出的槽位可以开始第二遍。
整个队列固定同一个 Snapshot 和配置；每次执行分别复制配置、导出 harness 文件，
Harbor 分别创建 trial 和环境。原始任务 ID 和 checksum 保持不变。
队列结束后按 repeat 分别评分和保存记录，避免同一任务的两次分数或 token 用量相互覆盖。
进度显示总计 `N * k` 次执行，并在任务标签中标明 repeat。
复用基线不重新执行任务；smoke 和最终 test 仍各执行一次所选任务。

`task_order: longest_first` 将两遍中预计较慢的执行优先派发。第一轮使用
基线/校准中每题的平均耗时；后续轮次使用上一轮所有完整候选测量的每题平均耗时，
包括被拒候选和返回错误的任务。同一轮所有 candidate 使用冻结的同一份估计，
下一轮开始时才更新。缺少新观测时保留旧估计；完全未知的任务使用已知任务估计的均值，
全部未知则保持 FIFO。同耗时按原始 repeat/任务顺序排列。
评分前恢复原始顺序，任务集合、重复次数、分数及 token 用量不会因排序而被覆盖。
`TaskExecutionTiming` 记录占用并发槽位的完整耗时，不包括排队；旧 Harbor 记录
可从保存的原生 `result.json` 读取起止时间。缺失或无效计时不阻断实验。
`measurement` 保存 `scheduling` 排序依据和每次执行的 `execution_seconds`。
smoke 和留出测试的用时不用于更新排序依据。

设置 `shared_candidate_queue: true` 后，同一轮所有通过检查的候选共用 Target 的
并发槽位。默认 `false` 保留逐候选评测；TB2.1 DeepSeek 示例已开启。
未开启流水线时，提案和 smoke 全部完成后才启动完整评测队列。沿用原有 repeat/任务排序，在每个位置
按候选生成顺序交错派发；`longest_first` 优先预计耗时长的任务，同耗时保持
repeat/任务/候选顺序。所有候选使用同一轮冻结的耗时估计和缺失值回退规则，
不会根据中途完成的候选更新估计或提前选择。

每项任务绑定轮次、候选编号、不可变 Snapshot、原始任务/checksum 和 repeat。
队列冻结 Target 配置，每次调用现有 adapter 的 `rollout` 时使用独立配置副本；
harness 导出目录和环境也各自独立。全局并发槽位覆盖清理全过程，等待槽位不计入
执行时间或任务超时。返回结果核对任务、`RunConfig` 中的快照和 Harbor verifier
身份，再按完整任务键归组。缺失、重复或错配会在等待运行中任务清理后中止本批；
普通任务错误仍按既有零分及错误比例规则处理。所有执行结束后，按候选生成顺序
分别计算分数、token 成本和归因，保留原有选择公式及同分优先级。
即使两个候选 Snapshot 相同，也分别完成独立的全部 trial。

`candidate-queue` 记录保存完整派发计划和全局并发上限；各候选的
`measurement.scheduling` 标明轮次和候选编号。进度显示整个队列，并标明候选、
快照前缀及 repeat。已有 baseline 和第一轮恢复入口允许切换此开关，并记录前后值；
逐 trial 检查点恢复则可以复用未完成轮次中的已完成任务，见文末恢复说明。
交错执行可能改变模型负载和随机输出，因此这里保证评测与选择规则一致，
不承诺模型响应逐字一致。

`proposal_concurrency`（默认 1）限制并行的候选生成阶段数量，覆盖编辑、修复、
critic 审查、输出预算有限重试和 smoke。候选编号预先固定，所有候选从同一
incumbent 开始，分别使用独立 checkout、冻结轮次上下文的私有副本和固定的
Worker Snapshot/配置。每次 Worker 调用独立导出 harness、创建会话和子进程。
已完成候选的结果不会进入兄弟候选的证据，也不会改变其预算、探索保留位或父版本。
Worker 记录附带轮次和候选编号。通过 smoke 后即释放生成槽位，再等待完整评测。

`pipeline_evaluation: true` 要求同时开启 `shared_candidate_queue`。
候选完成生成并通过检查后，立即把全部 `N * k` 次执行加入队列，
与其他候选的生成重叠；生成并发设为 1 时也可流水线运行。
关闭此开关仍可并行生成，但要等全部提案完成后才开始完整评测。
默认关闭；TB2.1 DeepSeek 示例设为 `true`，生成并发为 2。

并行生成和流水线共用一个 Target 队列，**smoke 与完整评测共同占用并发槽位**。
等待中的 smoke 优先，避免新候选的检查一直排在旧候选完整评测之后。
完整评测在当前已就绪任务中按冻结的耗时排序，同优先级按候选编号排列。
后来就绪的候选可以改变尚未派发任务的顺序，不抢占运行中的任务，
也不为等待未来候选而闲置槽位。任务计时从派发开始，原生清理完成后才释放槽位。
示例允许最多 8 个 Target 任务（包含 smoke）与 2 个候选生成阶段重叠，
因此 Worker 和 Target 会同时使用模型，实际提速取决于模型吞吐和任务耗时。

编译/smoke 失败或 Worker 输出重试耗尽，只跳过对应候选。
其他基础设施错误或主动取消会停止流水线，并等待 Worker、Target 及其临时目录清理。
所有候选生成和合格 trial 完成后，才按原候选编号评分、估算成本、归因和选择，
下一轮才更新耗时估计；同分优先级不受完成顺序影响。
`candidate-ready` 记录检查结果，`candidate-queue` 记录每次就绪批次，
`candidate-dispatch` 记录实际派发顺序、阶段、轮次、候选、快照和 trial 身份。
队列进度统计已提交任务，候选就绪时总数相应增加。
已有 baseline/第一轮恢复入口允许调整这两个配置并记录前后值。
新的逐 trial 恢复入口保持配置不变，可以复用半轮流水线中已完成的候选和任务。

## 分数、失败、成本与校准

默认对全部 train trial 等权求均值；也可用 `weight_metric` 指定每个 trial 都必须提供的严格正权重。状态为 error 的 trial 奖励置零，仍保留在分母中；其比例超过 `invalid_missing_frac`（默认 15%）则测量无效。权重或 guard 指标缺失也会使测量无效。基线无效则中止，候选测量无效则拒绝该候选；Worker 输出截断按下述有限恢复策略处理，其他 Worker 失败和框架协议错误仍向外传播。仅缺少 token 用量不会使测量无效。

成本为每次完整 Target 任务执行的 token 总数均值，包括缓存 token 和已报告的内部辅助模型/子 Agent 用量，不包括演化 Worker 调用。读取器支持原生 usage 格式，优先采用非空 SDK `model_usage` 会话汇总，避免只算父 Agent 或重复计算总量，不能推断未报告用量。这是 token 成本，不是金额或耗时。

用量不完整或均值非正时，`cost` 保存为 null，`cost_accounting` 单独记录完整/部分/缺失用量的 trial 数、已观测 token 总量及其按**全部 trial**计算的均值。后者只是下界，不是对缺失消耗的估计。比较双方有任一方缺少可用的完整成本时，仅在分数提升严格超过 `delta`、满足历史分数底线和领域 guards 时接受候选；不能依靠表面成本下降或新颖性单独晋升，`delta_C` 记为 null。双方用量完整时仍使用原 RRSI 成本公式。这是本地对缺失用量的容错扩展，不等同于上游完整成本选择规则。

MiniAgent 在每次模型返回后将 usage 刷入实时 trace。超时或子进程异常退出时，外层先停止并回收子进程，再恢复有效 trace 条目，并标记 `accounting_complete: false`；末尾损坏行会跳过。服务端尚未返回 usage 的中断请求仍为未知，不能恢复其精确消耗。汇总会计入恢复的已知用量，并单独显示明确标记用量不完整的记录数，不伪造零用量或估计值。

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

Worker 明确因单次输出上限而截断（`length` 或 `max_output_tokens`）时，受影响阶段最多重新执行 `worker_output_retries` 次（默认 1，即合计两次尝试）。阶段分为每轮分析（包含 digest）和每个候选的提案（包含修复及 critic 审查）。候选重试从 incumbent 的全新目录开始，丢弃半成品修改；分析重试先等待并发 digester 清理完成，丢弃不完整报告。重试提示要求尽早执行具体动作、简洁返回规定结构。模型设置、推理模式、单次输出上限和每次 Worker 调用的超时保持原值；新的调用仍可能消耗完整时间及 token 配额。候选重试耗尽后记为未评测的拒绝候选，继续兄弟候选；分析重试耗尽后跳过该轮并保留 incumbent 和最佳成绩，继续后续轮次。设 `worker_output_retries: 0` 可直接跳过。上下文超限、鉴权、基础设施错误与主动取消不按此策略吞掉。

失败及重试的 Worker 轨迹保留已观测用量。`worker-output-budget` 记录阶段、轮次索引、候选索引、尝试次数、错误及 retry/skip 动作；分析跳过另标记在 `rrsi-context` 和 `round-summary`，候选跳过记入 `edit-history`，不虚构候选分数。

以上门禁通过后，编译候选中的全部 `.py` 文件，再按 train 顺序执行最多 `smoke_n` 道已选代表任务。error 状态使 smoke 失败，这里不要求分数提升。`smoke_n: 0` 只编译、不执行。编译或 smoke 失败直接返回无效候选，不重新进入该候选的修复循环。临时证据文件在资源提交前移除。

digest 引用必须匹配某个解码后轨迹值的原文子串，轨迹文件必须保持不变。每份 digest 上限 6,000 字符、最多三次协议尝试；analyst 交互最多 30 轮。这些是协议常量，不是可配置搜索预算。

## 参数与成本

默认值放在 `algorithm` 下，RRSI 会拒绝未知算法参数。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `T` | `20` | 搜索轮数，整数，至少 1。 |
| `k` | `2` | 每次全量 train 测量的重复数，整数，至少 1。 |
| `task_order` | `fifo` | 原顺序，或 `longest_first`：按上一轮每题耗时优先派发慢任务。 |
| `shared_candidate_queue` | `false` | 候选共用 Target 并发槽位，全部完成并恢复原始结果顺序后再选择。 |
| `proposal_concurrency` | `1` | 并行候选生成阶段上限，包含审查、重试和 smoke。 |
| `pipeline_evaluation` | `false` | 就绪候选立即评测，与兄弟候选生成重叠；要求开启共享队列。 |
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
| `worker_output_retries` | `1` | Worker 输出截断后的额外阶段尝试次数，整数，至少 0；耗尽后跳过候选或该轮。 |
| `invalid_missing_frac` | `0.15` | 允许的 error trial 比例上限，范围 `[0, 1]`。 |
| `digest_concurrency` | `1` | 同一批独立轨迹调查的并发上限；analyst 每批最多请求 8 条。 |
| `seed` | `7` | bootstrap 整数随机种子，不控制模型采样。 |
| `n_fail_traces` | `22` | 低分代表任务数，整数，至少 0。 |
| `n_success_traces` | `6` | 高分代表任务数，整数，至少 0；两类请求总数必须为正。 |
| `calibration_evals` | `1` | 自动校准时基线测量总次数，整数，至少 1。 |
| `smoke_n` | `2` | 每个候选 smoke 的代表任务数上限，整数，至少 0。 |
| `component_signals` | `{}` | 语义标签到非空正则列表的映射，不是文件权限。 |
| `weight_metric` | `None` | 可选的逐 trial 正权重指标。 |
| `guards` | `{}` | 指标名到非负 `max_drop`/`max_rise` 限制的映射。 |

设 train 有 N 题，每次完整测量执行 `N * k` 次 Target 任务。自动 delta 时基线/校准成本为 `N * k * calibration_evals`，显式 delta 时为 `N * k`。完整候选测量最多 `T * m * N * k` 次，候选 smoke 最多额外 `T * m * min(smoke_n, N, n_fail_traces + n_success_traces)` 次。无效提案会减少这些次数；最终 test、重试和原生模型/工具轮次另计。

Worker 成本独立计算：每次候选阶段尝试最多编辑 `repair_rounds + 1` 次，每次到达 critic review 时最多调用三次；输出截断可使整个阶段最多尝试 `worker_output_retries + 1` 次。每轮还有 analyst 和 digest 调用，每批 digest 请求最多调查八条轨迹，分析阶段同样受上述重试上限约束。机制预算不限制这些调用的总 API 请求数。

## 记录与适配

`measurement` 记录分数、成本、trial 证据和 guard 指标，`calibration` 记录噪声带，`rrsi-context` 固定共同的轮次证据。`critic`、`repair`、`smoke-gate` 解释提案处理。`edit-history` 保留完整 diff、`admissible`、`accepted`、`ACCEPTED/LOST/REJECTED`、分数/成本变化及归因。最终 `promote` 保存最终分数/成本、历史最高分和 delta。

归因根据逐题均分变化核对预测任务，并标记至少下降 `1/k` 的未预测退步。未知预测任务 ID 不进入命中率分母。捆绑编辑共享一次候选测量，归因不是每项机制的因果消融。无效或未测候选保留原因，但不计入已测组件尝试与收益统计。

[实现](../flashrsi/algorithms/rrsi/__init__.py)独立移植 [google-research/rrsi](https://github.com/google-research/rrsi/tree/be50316e1db05914068a973f322770ef08ed7ba1) 的机制，固定版本为 `be50316e1db05914068a973f322770ef08ed7ba1`。执行、评分、编辑、版本管理和记录分别复用 Agent、Benchmark、Worker、Snapshot、Algorithm。未复制上游 Git 管理、Domain adapter、角色专用模型配置、恢复/重评命令与起始 harness。SearchQA/MiniAgent 示例是本地适配，不是原始 coding 实验配置。

## 完整轮次与逐 trial 恢复

RRSI 自动写入 `checkpoints/`：完整轮次状态、已完成的分析/候选生成阶段，
以及每个完成并清理环境后的 Target trial。检查点使用校验和及原子替换。
完整轮次状态同时包含当前测量、历史最优、delta、编辑历史、分析及耗时估计。

用原配置恢复到一个新的运行目录：

```bash
python -m flashrsi run --config experiment.yaml --resume-from /path/to/source-run --resume-mode trial
```

- `trial`：复用已完成阶段和 trial，仅执行缺失部分；完成的失败 trial 仍保留失败结果。
- `round`：保留最后完整轮次，丢弃未完成轮次的进度，从该轮重新生成和评测。

对应配置键为 `run.resume_from`、`run.resume_mode`，恢复路径相对当前工作目录。
`--check-only` 校验配置、任务及记录，不执行推理。TB2.1 脚本也支持这些恢复模式；
其默认 `auto` 优先使用逐 trial 检查点，旧日志则恢复最后完整轮次，只有基线的日志
继续使用原有 baseline 恢复。原有 `--reselect-round-one` 仍是独立的第一轮重选入口。

trial 身份绑定轮次、候选编号、阶段、重复位置、任务内容/checksum、配置和 harness。
即使两个候选生成相同的 harness，也分别保留它们的测量结果。
所有完整候选仍按原编号聚合与选择；恢复不会用先完成的候选提前结束一轮。
未完成的 Worker 阶段从 incumbent 重新开始，不恢复半写的编辑、模型对话或 KV cache；
已完成的候选（包括被拒绝的候选）不重新生成。完成前被中断的整个 trial 会重新执行。
原运行目录只读，恢复使用新目录；损坏记录或任务、模型、预算、算法/调度配置不匹配
会在推理前拒绝恢复。运行凭据和产物目录可以变化，`T` 因影响退火预算必须保持一致。

没有新检查点的旧运行，只能恢复经过完整性校验的完整轮次，不能凭零散容器产物复用
半轮 trial。
恢复元数据记录在 `resume.json`；完成测量按原顺序重新汇总，避免重复计入 trial。
中断请求尚未报告的用量仍未知。新运行也支持在进化完成、最终留出集评测中断后复用
已完成的最终测试 trial；独立 `evaluate` 命令的运行方式保持原样。
