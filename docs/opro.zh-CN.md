# OPRO

[English](./opro.md) · **简体中文**

[算法总览](./integrations/algorithms.md) · [示例配置](../examples/opro.yaml)

OPRO 把历史指令及其分数展示给 Worker，让它提出更好的指令。所有评估过的指令都留在搜索历史中。搜索期间 Target 的活动 Snapshot 保持不变，只有最终选择才激活最佳版本。

## 使用条件

Target 必须声明属于 `prompt` 组件的 `instruction_file`。OPRO 替换该文件的完整文本，保留 Snapshot 的其他资源。Worker 可以是通过 `ask()` 返回文本的纯 LLM 或 Agent。

train 必须非空。**当前实现使用 train 评分和选优**，不设 dev 接受门槛。最终 test 评估由 runner 负责，test 反馈不进入候选生成。

## 运行小示例

按[安装指南](./guide/installation.md)准备环境。在 [examples/quickstart.yaml](../examples/quickstart.yaml) 中分别配置 Target 和 Worker 的模型与凭据，然后从仓库根目录使用其内置本地 QA 数据运行：

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm opro \
  --algorithm.max_steps 1 \
  --algorithm.num_candidates 2
```

追加 `--check-only` 可检查初始化，不调用模型，也不验证远端认证。算法专用的 SearchQA 配置见 [examples/opro.yaml](../examples/opro.yaml)，其默认搜索预算大得多。模型设置和命令行覆盖见[配置与 CLI](./guide/configuration.md)。

## 搜索与选择

1. 在完整 train 上评估初始 Snapshot，将其指令作为第一条历史记录。
2. 每步选择历史中分数最高的条目作为父版本，同分保留较早条目。
3. 从达到 `score_threshold` 的条目中取分数最高的一部分，按分数升序展示；同时采样当前 train 最佳版本的任务、输出和指标作为提示证据。
4. 用该提示调用 Worker `num_candidates` 次，解析 `<INS>...</INS>` 回复。空指令和重复指令直接丢弃，不重新采样。本步全部提案生成后才开始评估。
5. 从本步父 Snapshot 构造每个新指令候选，在完整 train 上评估。所有候选都加入历史，包括低分候选；严格更高分的结果成为下一步证据来源。
6. 完成 `max_steps` 后，提升最早出现的全局最佳指令；如果初始版本仍最佳，则保留它。

历史阈值只过滤 Worker 看到的条目，不删除已存历史，也不限制最终选优。展示分数乘以 100 后取整，实际选择仍使用原始分数。

回复解析器读取最后一对完整的 `INS` 标签；没有完整标签时，使用整条回复作为指令文本。

## 参数与成本

以下字段放在 `algorithm` 下，列出的是实现默认值，而非小示例的覆盖值。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `max_steps` | `200` | 提案步数；整数，至少 1。 |
| `num_candidates` | `8` | 每步 Worker 提案调用次数；至少 1。 |
| `history_size` | `20` | 展示给 Worker 的合格历史条目上限；至少 1。 |
| `score_threshold` | `0.1` | 进入展示历史所需的最低原始分数。 |
| `few_shot_size` | `3` | 每个提示采样的最佳版本轨迹数，不超过 train 大小；至少 1。 |
| `max_evidence_chars` | `24000` | 序列化证据块的字符上限。 |
| `seed` | `42` | 整数随机种子，证据采样使用 `seed + step`。 |

设 train 大小为 N、步数为 S、每步提案数为 C，搜索最多执行 `(1 + S * C) * N` 次 Target 任务，包含基线。空提案或重复提案会减少评估次数。正常完成时，Worker 提案调用共 `S * C` 次。最终 test、原生工具/模型轮次以及后端重试另计。

## 查看实验记录

最终 `promote` 决策包含 `score`、`baseline_score` 和完整 `history`，后者记录指令文本、分数及 Snapshot ID。结合 `baseline` 和 `candidate` 任务轨迹阅读。历史条目表示评估过的提案，不代表激活过的版本；搜索中间不会提升版本。

## 来源与适配

[提示模块](../flashrsi/algorithms/opro/_prompts.py)将 `google-deepmind/opro` 固定在 commit `a76bdce2cbf6d4a0d1e570a6fcfe17be9c2abdd7`。本地适配编辑一个原生指令文件，不实现上游向题目中插入指令的多个位置；提示证据使用从 FlashRSI 轨迹采样的任务输出和指标。当前行为见[搜索实现](../flashrsi/algorithms/opro/__init__.py)。

## 验证范围

上述命令是小规模运行示例，不是已报告的实验结果。初始化不能验证提案质量或完整演化。有效的 smoke 应记录是否生成新指令、train 评估是否完成，以及最终提升是否遵循评分历史。train 分数提高本身不代表泛化提升，最终 test 应单独报告。
