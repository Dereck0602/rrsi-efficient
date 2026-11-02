# AHE

[English](./ahe.md) · **简体中文**

[算法总览](./integrations/algorithms.md) · [示例配置](../examples/ahe.yaml)

AHE 编辑当前 harness，声明每项改动预期影响哪些任务结果，再用下一次 train 评估核对预测。协议合法且完成评估的编辑全部保留，包括降分版本。下一轮 Worker 根据判决决定保留、改进或撤销。最终结果是最后保留的版本，不是 dev 最佳版本。

## 使用条件

train 必须非空且任务 ID 唯一。Worker 必须是可编辑文件的 Agent（`algorithm.worker.type: agent`），纯 LLM Worker 在启动时报错。Target 和 Worker 独立配置模型与连接。

`evolve_scope = {"*"}` 覆盖 Target 声明的全部组件，不代表任意宿主文件或所有 harness 的内部实现。MiniAgent 可以暴露运行时代码，Claude Code 暴露支持的指令、扩展与设置。新文件必须满足 Target 的组件规则，详见 [Harness 接入](./integrations/harnesses.md)。

AHE 用 train 同时诊断和评分，不使用 dev，也不恢复 dev 最佳版本。最终 test 由 runner 负责，test 证据不进入 debugger 或编辑提示。

## 运行小示例

按[安装指南](./guide/installation.md)准备环境，在 [examples/quickstart.yaml](../examples/quickstart.yaml) 中填写两个角色的模型与凭据。从仓库根目录运行：

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm ahe \
  --algorithm.worker.type agent \
  --algorithm.worker.name miniagent \
  --algorithm.worker.max_turns 30 \
  --algorithm.max_steps 1 \
  --algorithm.use_debugger false
```

示例使用内置本地 QA 数据，省去可选 debugger 调用，仍评估完整 train。如果初始 train 分数已达到 `target_score`，则直接停止，不编辑。追加 `--check-only` 可检查初始化，不调用模型，也不验证认证。专用 [AHE 示例](../examples/ahe.yaml)默认启用 debugger。

## 搜索与选择

1. 在完整 train 上评估初始 Snapshot。
2. 每次尝试前，若当前均分达到 `target_score`，则停止。
3. 汇集当前训练轨迹、上一轮改动判决、有长度限制的回滚原文，以及最近四次拒绝记录。启用 `use_debugger` 时，先逐任务诊断，失败优先，再生成跨任务总览。
4. 检出 current，由 Worker 编辑文件。清理已知运行时产物，在组件规则内提交，再将真实改动路径与返回的 manifest 对照。非法或空编辑消耗一次尝试，不评估候选。
5. 在相同完整 train 上评估合法候选，记录逐题前后分数、分差与成败翻转，并判决 manifest 预测。
6. 即使同分、降分或得到无效/有害判决，仍激活候选，作为下一轮编辑基线。尝试耗尽或分数达标后返回最后保留的 Snapshot。

当前未实现自动回滚或不退步门槛。回滚是后续 Worker 可以做出的编辑，参考此前判决和原文。回滚文本受长度限制，二进制内容不渲染为文本。Worker/debugger 执行失败向外传播，不视为普通编辑拒绝。

## Manifest 与判决

编辑回复要求在 `<MANIFEST>...</MANIFEST>` 中返回 JSON：

```json
{
  "failure_evidence": ["task and trace evidence"],
  "root_cause": "why the harness failed",
  "targeted_fix": "what changed",
  "changes": [
    {"path": "relative/file", "fix_tasks": ["task-id"], "risk_tasks": []}
  ]
}
```

声明路径必须与实际 diff 完全一致。`changes` 缺失或为空时，将顶层 `predicted_impact` 展开到各改动文件。每个文件省略的 fix/risk 列表继承整体预测，显式空列表仍为空。缺少必要字段、非法 JSON 或路径不符会被拒绝。标签解析器也接受无标签的 JSON 回复。

成败归因以零为边界：`before <= 0 < after` 为 `fail_to_pass`，`before > 0 >= after` 为 `pass_to_fail`。其他变化在类别判决中为 `unchanged`，但仍记录数值分差。因此 `0.3 → 0.8` 不算一次预测修复。

| 判决 | 对单个声明文件改动的含义 |
| --- | --- |
| `EFFECTIVE` | 预测修复集合非空且全部翻转，没有声明风险发生。 |
| `PARTIALLY_EFFECTIVE` | 部分预测修复翻转，没有声明风险发生。 |
| `MIXED` | 至少一个预测修复和一个声明风险发生。 |
| `HARMFUL` | 声明风险发生，没有预测修复发生。 |
| `INEFFECTIVE` | 没有预测修复或声明风险发生。 |

未出现在任何改动 fix/risk 列表中的回归，记为 `unattributed_regressions`。这是对联合执行结果的预测核对，不是能证明某个文件导致变化的受控因果归因。

## 参数与成本

以下默认值放在 `algorithm` 下。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `max_steps` | `10` | 编辑尝试上限；整数，至少 1，非法尝试也计数。 |
| `target_score` | `1.0` | 当前 train 均分达到此值时停止。 |
| `use_debugger` | `true` | 增加逐任务诊断和跨任务总览。 |
| `debugger_max_tasks` | `90` | 每次最多诊断的轨迹数，失败优先；至少 1。 |
| `max_evidence_chars` | `30000` | 序列化证据块字符上限；至少 1。 |

设 train 有 N 题、最多 S 次尝试，Target 搜索最多执行 `(1 + S) * N` 次任务：一次初始基线，加每个合法候选的一次评估。当前分数复用，不会每步重新跑基线。每次尝试调用一次编辑；启用 debugger 时再增加最多 `min(N, debugger_max_tasks)` 次逐题调用和一次总览。原生模型/工具轮次及最终 test 另计。

## 查看实验记录

`decision.kind: decline` 记录非法尝试及原因。`kind: keep` 记录父/候选分数、manifest、逐题归因、判决、回滚来源及 `accepted: true`。接受表示保留资源，不代表性能提升。AHE 不以选最佳版本的 `promote` 决策结束。

## 来源与适配

[实现](../flashrsi/algorithms/ahe/__init__.py)、[manifest 协议](../flashrsi/algorithms/ahe/_manifest.py)和[提示](../flashrsi/algorithms/ahe/_prompts.py)记录上游为 `china-qijizhifeng/agentic-harness-engineering` commit `8b2a55d97590363fe50c3cc6b5e833b020a4bb4c`，经 FlashRSI `5efe9b438483710060183c6c3ecbc5062b410a58` 迁移。原迁移记录未独立核验继承的上游版本。

本地取舍包括回复内 manifest 替代 checkout 内文件、核对真实 Snapshot diff、限制证据长度，以及非法编辑后继续。保守回滚/不退步变体和跨进程搜索恢复未实现，添加额外配置键不会产生这些能力。

## 验证范围

本地历史报告 `docs/smoke-ahe-20261023.json` 记录 MiniAgent × HotpotQA 的单训练题、单候选流程。基线和候选均通过，预测修复判为 `INEFFECTIVE`，没有证实收益，也未覆盖后续判决反馈或回滚轮次。报告还记录代码 docstring 含训练示例/任务 ID，不能据此声称泛化。

报告是本地被忽略的产物，不是公开结果链接，也不覆盖所有组合。本次文档更新未增加模型调用。有效的多步验证应展示下一轮 Worker 确实读取此前判决；合法编辑和执行成功本身不代表提升。必须保留原生执行接口：运行时/输出协议错误仍可能终止搜索，而非变成可评分候选。
