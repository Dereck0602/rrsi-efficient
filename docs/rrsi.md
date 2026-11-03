# RRSI

按[官方代码固定版本](https://github.com/google-research/rrsi/tree/be50316e1db05914068a973f322770ef08ed7ba1)
独立移植搜索机制。执行、评分、目录编辑、版本管理和记录分别复用现有的
Agent、Benchmark、Worker、Snapshot 和 Algorithm；没有增加框架公共接口。

## 从 `evolve()` 阅读算法

1. 在整个 `train` 上评估当前 Snapshot，每题运行 `k` 次，得到分数和目标 token 成本；
   `delta: null` 时用基线的重复 trial 校准噪声带。
2. 选择低分任务最差 trial、高分任务最佳 trial。analyst 只看任务表，调度 digester
   通过 Worker 文件工具调查完整脱敏轨迹，再汇总 failure modes、capability gaps
   和 success habits。支持三种调查视角和追问，保留前轮模式名称。
3. 计算余弦退火编辑预算、停滞探索槽和无近期收益的 prune 建议，将分析、代表轨迹
   和完整编辑历史保存为一个固定的本轮 context。
4. 每个候选从同一当前 Snapshot 出发，读取同一 context。Worker 编辑 checkout，
   声明独立机制及其预测影响；critic 审查完整 diff，可在原 checkout 修复。
   Snapshot 检查文件权限与运行凭据，之后编译 Python 并通过目标原生 rollout 做 smoke。
5. 评估候选并核对预测，按 RRSI 的分数／成本规则选出最高分的可接受候选。
   全部候选完成后才移动当前版本，记录每项编辑的结果与归因，进入下一轮。

预算限制独立机制数量，一个机制可以涉及多个文件。每项声明包含
`id/component/hypothesis/targets_mode/predicted_affected/retroactive_check/paths`，
必须覆盖真实修改。九种 RRSI 标签用于探索、历史和新颖性，可编辑范围只有
Snapshot 的组件声明这一份。标签根据各项编辑自己的文件 diff 校正，可选
`component_signals` 补充目标 harness 的机制信号；critic 判断接线与独立性。
prune 是接受相同审查与测量的删除提案，不自动删除当前版本。

修复在同一个 checkout 内累计修改，返回的清单必须覆盖相对 incumbent 的全部
差异，包括之前尝试留下的文件；漏报错误会列出缺失路径。一个机制及其配置接线
归在同一项声明中，不按文件拆成多项。digester 的 `evidence[].quote` 必须是
脱敏 trace 中某个解码后 JSON 值的原文子串；字段路径放在 `where`，不能把
序列化的键值对或自行拼出的指标描述当作引用。
analyst 表格中的 `rewards` 列出各次试验分数，但每个任务只提供 `trial/reward`
指明的代表轨迹；切换调查视角不会切换试验。其他试验的具体过程不可从汇总分数
还原，报告应保留证据缺口，不重复请求 digester 比较未提供的轨迹。

编辑历史是唯一反馈账本，包含完整 diff、胜负、分数／成本变化与 `attribution`。
预测命中和未预测退步按逐题均值核对，退步阈值固定为 `1/k`；未知预测任务不进入
命中率分母。捆绑机制共享一次候选测量，归因不代表逐项因果消融。
无效或未评测候选保留失败原因，但不进入已尝试组件和收益统计。

## 选择规则

设 `dS = S' - S_t`、`dC = (C' - C_t) / C_t`：

- `S' < S* - delta`：拒绝；历史最佳分数 `S*` 只随接受版本提高。
- `dS > delta`：要求 `dC <= beta0 + beta1*dS`。
- 其余情况：要求 `w_s*dS - w_c*dC + w_n*novelty > 0`。
- `guards` 中的指标变化约束也必须通过。可接受候选按分数选择，平分保留较早者；
  全部拒绝时保留当前版本。

novelty 仅计算尚未出现在获胜编辑中的 `client_tool/skill/memory/subagent` 类型。
停滞由过去 `w` 轮的分数变化判断，最后 `m_draft` 个槽要求实际探索未测过的组件。
预算为 `ceil(b_min + (b_max-b_min)*(1+cos(pi*t/T))/2)`。

主指标须在 `[0,1]`，失败 trial 奖励为零并留在分母中；失败比例超过
`invalid_missing_frac` 则测量无效。默认等权；`weight_metric` 与 `guards` 可读取
Benchmark 已评分的 `Trajectory.metrics`，统计缺失会拒绝测量。runner 的最终 test
继续遵守框架 `Results.score` 的主指标算术均值契约，没有额外的加权 test 分数支路。
使用加权演化时，最终 test 的均值不能直接当作同一加权协议的结果。

成本使用每次完整目标运行的 token 总数，包括缓存及目标内部辅助模型／子 agent，
不包含演化 Worker。支持现有 MiniAgent、OpenAI 与 Claude SDK usage 形式，优先采用
非空 SDK `model_usage` 会话统计，避免只计算父 agent。任一 trial 统计不完整或成本
均值非正就拒绝；基线无效则终止。框架协议错误与 Worker 调用失败继续向外传播。

自动校准默认按任务内 trial 做 2000 次 bootstrap，取
`delta_z * sqrt(2) * std(bootstrap scores)`。`calibration_evals > 1` 可使用重复基线
评测的样本标准差，退化时回退到 pooled bootstrap；要求 `k >= 2` 或至少两次校准。
校准不挑选最好一次作为基线；也可显式提供实测 `delta`。

## 配置与边界

运行 [示例](../examples/rrsi.yaml)：`uv run flashrsi run --config examples/rrsi.yaml`。
搜索参数沿用上游符号，其余默认值见 `RRSI._defaults`。分析交互上限固定 30 次，
digest 上限 6000 字符，格式错误最多修复 3 次；这些协议常量不再作为配置项。
`repair_rounds` 控制提案修复次数，`smoke_n` 控制原生 smoke 任务数，0 时只编译。
取消 `component_paths/critic_patterns/max_review_chars/briefs/constitution/
analysis_max_turns/digest_max_chars/protocol_retries/smoke_task_ids/regression_threshold`
等额外配置；旧键会明确报错。Worker 的模型、提示与执行配置仍由框架提供。

代码只保留两个文件：`__init__.py` 实现算法，`_prompts.py` 存放长提示词。
`evolve()` 呈现基线评测、调查、提案与选择；这些算法步骤用同一个类的私有方法
实现，数值规则和编辑解析留在文件下方。评测聚合直接在 `_measure()` 内完成，
没有候选状态类、独立评估层或并行 scoreboard。角色调用由框架自动记录，完整
分析在 round context，归因在 edit history；摘要数量直接从列表计算。
提案读取临时 `context.json` 和 `current-diff.txt`；后者提供修复开始时相对 incumbent
的累计差异，首次为空，帮助 Worker 保留此前编辑的清单。两者随 checkout 移动并在提交前清理；原文引用
与只读 trace 仍检查，不为角色回复搭建完整字段校验体系。
critic 区分清单中必需的任务归因和版本文件中的任务记忆；也不把 diff 未包含的
原生加载器误判为不存在。任务特化、未接线机制等规则不变，拒绝理由需要对应实际文件证据。

没有复制上游 git 管理、Domain adapter、角色模型配置、恢复／重评命令及三套起始
harness。示例使用 SearchQA 和 MiniAgent，属于框架内的机制验证。官方 coding 实验
使用 Terminus-2、Terminal-Bench 2.1 的 89 个 evolve 任务及
`T=20/k=2/m=2/w=3/delta=0.017/beta1=44.5/w_s=0/repair_rounds=5/invalid_missing_frac=0.2`。
框架默认 Terminal-Bench 数据仍是 2.0；严格复现需要核对起始 harness、数据、模型
和执行限制。
