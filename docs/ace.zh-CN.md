# ACE

[English](./ace.md) · **简体中文**

[算法总览](./integrations/algorithms.md) · [示例配置](../examples/ace.yaml)

ACE 通过 Generator → Reflector → Curator 流程积累 playbook。合法增量和使用计数变化立即被采用，允许学习在分数下降时继续。Offline 定期评估 dev 并返回测得的最佳版本，online 保留最终积累的 playbook。

## 使用条件

Target 必须声明属于 `prompt` 组件的 `instruction_file`。Worker 可以是纯 LLM 或 Agent：反思与策展都通过 `ask()` 获取回复，不需要目录编辑。

train 必须非空，offline 还要求 dev 非空。设置了 `Task.env` 的环境任务要求 `max_reflection_rounds: 1`。多轮重试通过在问题前添加诊断实现，尚无通用环境注入协议，因此该组合在启动时报错。

两种模式都向 Reflector 提供训练 `ground_truth`（若有），online 不等于无标签在线复现。Dev/test 轨迹不交给 Reflector 或 Curator，最终 test 由 runner 负责。

## 运行小示例

按[安装指南](./guide/installation.md)准备环境，在 [examples/quickstart.yaml](../examples/quickstart.yaml) 中独立配置两个角色，然后从仓库根目录使用其本地 QA 数据运行：

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm ace \
  --algorithm.mode offline \
  --algorithm.num_epochs 1 \
  --algorithm.max_steps 2 \
  --algorithm.eval_steps 1 \
  --algorithm.max_reflection_rounds 1
```

按原顺序在线消费：

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm ace \
  --algorithm.mode online \
  --algorithm.num_epochs 1 \
  --algorithm.max_steps 2 \
  --algorithm.max_reflection_rounds 1
```

命令沿用 `batch_size: 2` 和 `edit_budget: 1`。追加 `--check-only` 可检查初始化，不调用模型，也不检查远端认证。专用的 [ACE 配置](../examples/ace.yaml)启用两轮反思。凭据和覆盖方式见[配置与 CLI](./guide/configuration.md)。

## 搜索与选择

1. 加载受管 playbook 后缀。Offline 在 dev 上评估初始 Snapshot。
2. 用 current 执行训练 batch。Online 在反思、重试或更新前记录首次尝试的分数。
3. 反思输出、分数、状态、错误与参考答案，正确尝试也参与。只要有任务未完成或低于 `success_threshold`，且还有反思轮数，就重试。每次重试使用原始问题加最新一次诊断，Snapshot 不变。Curator 读取最后诊断及其对应尝试。
4. 将 helpful/harmful 标签应用到 bullet 计数，变化后立即采用。Neutral 或未知标签不改变计数。
5. 每 `curator_frequency` 个 batch，用当前批次最后的诊断和证据请求 ADD 操作。合法变化直接采用，不设成绩门槛。非法操作及大小/预算违规被拒绝，最近四次拒绝供后续策展参考。
6. Offline 每 `eval_steps` 个 batch 评估 dev，即使策展被跳过或拒绝也照常进行。只有严格更高分才替换最佳版本。结束时若 current 与上次验证版本不同，则补评，再恢复测得的最佳 Snapshot。
7. Online 跳过 dev 和恢复，返回 current。Runner 在 test 上评估所选 Snapshot。

### Offline 与 online

| 行为 | Offline | Online |
| --- | --- | --- |
| 顺序 | 按 `seed + epoch` 打乱 | 每次遍历保持原顺序 |
| 遍历次数 | `num_epochs` | 当前实现也使用 `num_epochs`；单次消费需设为 1 |
| Dev | 初始、周期及必要的结束补评 | 不访问，可以为空 |
| 最终版本 | 测得的最佳 dev Snapshot | 最后积累的 Snapshot |
| 汇总 | `promote` 中的 dev `score` | `stream` 中的 `online_score` 和 `online_tasks` |

`online_score` 是每次消费任务时首次尝试的均分，在该批更新 playbook 前计入，不含重试。`num_epochs > 1` 会重复消费并重复计数；单次在线报告应设 `num_epochs: 1`。流式指标与最终 `test_score` 衡量不同表现，必须分别报告。

## Playbook 中哪些内容会改变

ACE 保留指令前缀，只管理 `<!-- flashrsi:ace-playbook -->` 后的后缀。已有后缀会复用，原指令的普通列表不转换成受管 bullet。无操作更新不改变资源。

策展仅支持 ADD。实现分配分节编号和初始计数，将多行内容收为一个 bullet，并按空白规范化与大小写折叠后的内容去重。条数限制在去重前检查。除 `general` 外，无法识别的新分节归入 `others`。模型不能自行指定有效的 bullet ID。

MiniAgent 通过原生指令文件加载 playbook；Claude Code 通过 `CLAUDE.md` 追加，同时保留原生 system prompt。算法不按 harness 名称分支，详见 [Agent 接入说明](./agent-integration.md)。

已识别的 Worker 轮数、时间或 token 耗尽记录为 `worker_budget_exhausted`。没有反思结果时，不应用标签，也不给 Curator 提供输入。策展失败时保留 current，包括此前已采用的计数变化。其他 Worker 失败向外报错。当前未实现语义去重、playbook 压缩和演化断点恢复。

## 参数与成本

以下实现默认值放在 `algorithm` 下。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `mode` | `offline` | `offline` 或 `online`，决定验证和选优。 |
| `num_epochs` | `1` | 两种模式的 train 遍历次数；整数，至少 1。 |
| `batch_size` | `1` | 每批训练任务数；至少 1。 |
| `max_steps` | `null` | 总 batch 上限；正整数或 `null`。 |
| `edit_budget` | `4` | 每次策展去重前的 ADD 上限；至少 1。 |
| `eval_steps` | `100` | Offline dev 间隔，以训练 batch 计；至少 1。 |
| `max_reflection_rounds` | `3` | 每批诊断次数，重试最多比它少一次；至少 1。 |
| `curator_frequency` | `1` | 每隔多少 batch 策展一次；至少 1。 |
| `playbook_token_budget` | `80000` | 后缀近似大小上限：`ceil(字符数 / 4)`，不含原始前缀；至少 1。 |
| `success_threshold` | `1.0` | 全部任务完成且分数达到此值时停止重试。 |
| `max_evidence_chars` | `24000` | 序列化训练证据字符上限；至少 1。 |
| `seed` | `42` | Offline 打乱的整数种子，online 不打乱。 |

每批任务最多执行 `max_reflection_rounds` 遍，进行同样次数的反思调用，外加按频率执行的 Curator 调用。Offline 还有初始、周期及可能的结束 dev 评估。`max_steps` 不是模型调用预算，`playbook_token_budget` 也不是 tokenizer 测量值。

## 查看实验记录

`tally` 和 `curate` 决策中的 `accepted: true` 表示立即采用，不代表 dev 提升。`declined` 标识非法策展或已识别的 Worker 预算耗尽。Offline 以 `promote` 结束，online 以 `stream` 结束。通过 Snapshot ID 区分最后学习到的版本与选中最佳版本，最终 test 单独报告。

## 来源与适配

[实现](../flashrsi/algorithms/ace/__init__.py)和 [playbook 协议](../flashrsi/algorithms/ace/_playbook.py)记录的迁移参考为 FlashRSI commit `5efe9b438483710060183c6c3ecbc5062b410a58`，上游为 `ace-agent/ace` commit `82709de050e1db6e6ef2f07bcb0393560b94992a`。原迁移记录未独立核验该上游版本，本页描述本地行为。

本地取舍包括用指令后缀替代 skill、在证据中明确提供训练答案、只在诊断之间重试、dev 调度独立于 ADD 是否成功，以及通过已有 Snapshot 恢复。未引入独立 Trainer/Recipe/checkpoint 层。[提示模板](../flashrsi/algorithms/ace/_prompts.py)定义回复协议。
