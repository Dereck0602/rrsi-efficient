# EvoPrompt

[English](./evoprompt.md) · **简体中文**

[算法总览](./integrations/algorithms.md) · [示例配置](../examples/evoprompt.yaml)

EvoPrompt 用遗传算法（GA）或差分演化（DE）提示演化指令种群。GA 生成整代子代后统一选出幸存者；DE 让每个试验候选只与其目标位置上的成员比较。两种变体都保留跨代遇到的最佳 dev 版本，供最终提升。

## 使用条件

Target 必须声明属于 `prompt` 组件的 `instruction_file`。Worker 可以是通过 `ask()` 返回提案的纯 LLM 或 Agent。算子替换指令文件的完整文本，保留 Snapshot 的其他部分。

dev 必须非空。GA 种群至少 2 个成员，DE 至少 4 个。当前实现使用 dev 评估适应度，不消费 train 轨迹。最终 test 评估由 runner 负责。

## 运行小示例

按[安装指南](./guide/installation.md)准备环境，在 [examples/quickstart.yaml](../examples/quickstart.yaml) 中独立配置 Target 与 Worker 的模型设置，然后从仓库根目录使用其内置本地数据运行：

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm evoprompt \
  --algorithm.variant ga \
  --algorithm.population_size 4 \
  --algorithm.max_steps 1
```

运行一代 DE 时，种群仍需至少 4 个成员：

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm evoprompt \
  --algorithm.variant de \
  --algorithm.population_size 4 \
  --algorithm.max_steps 1
```

追加 `--check-only` 可检查初始化，不调用模型，也不验证模型认证或任何算子。算法专用的 [SearchQA 配置](../examples/evoprompt.yaml)使用更大的默认值。凭据和覆盖方式见[配置与 CLI](./guide/configuration.md)。

## 搜索与选择

1. 在 dev 上评估初始 Snapshot。让 Worker 改写其指令，填充剩余种群位置；对每个有变化的指令评估完整 dev。
2. 每代使用所选 GA 或 DE 算子，随机种子为 `seed + step`。
3. 按该变体的幸存策略更新活动种群，同时单独保存全局最佳版本。只有 dev 分数严格提高时才替换这个最佳版本，同分保留此前选中的版本。
4. 初始化后完成 `max_steps` 代，最后一次性提升全局最佳版本。种群搜索期间不切换 Target 的活动 Snapshot。

### GA

从父版本对生成 `population_size` 个子代。`wheel` 先按适应度权重对种群有放回重采样；全部适应度为零时使用均匀权重。`random` 均匀抽取父版本对。`tournament` 为每个父版本抽取两个成员，取其中更优者；`tour` 是其别名。

`survivor_selection: topk` 将原始父代与子代合并排序，先保留最优的不同指令，再按需用剩余最优条目补齐种群。`standard` 则让全部子代成为下一代。即使 `standard` 将全局最佳移出活动种群，单独保存的最佳版本仍会保留。

### DE

对每个目标位置，从其他成员中采样三个不同的 donor 位置。提示将 donor 差异、基底指令和目标结合起来。默认基底为当前代最佳成员；`donor_random: true` 使用第三个采样 donor。所有试验候选都使用本代开始时的原始种群。

试验候选只有在 dev 分数严格提高时，才替换自己的目标成员。同分保留该目标。GA 的 `selection` 与 `survivor_selection` 不控制 DE。

## 参数与成本

所有字段均放在 `algorithm` 下。默认规模大于上面的小示例。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `variant` | `ga` | `ga` 或 `de`。 |
| `population_size` | `10` | 种群位置数，GA 至少 2，DE 至少 4。 |
| `max_steps` | `10` | 初始化后的代数；整数，至少 1。 |
| `selection` | `wheel` | GA 父版本策略：`wheel`、`random` 或 `tournament`（别名 `tour`）。 |
| `survivor_selection` | `topk` | GA 幸存策略：`topk` 或 `standard`。 |
| `donor_random` | `false` | DE 使用随机的第三个 donor 代替本代最佳作为基底。 |
| `seed` | `42` | 每代父版本/donor 采样的整数随机种子。 |

设种群大小为 P、代数为 G、dev 大小为 D，搜索最多执行 `P * (1 + G) * D` 次 Target 任务。正常完成时，Worker 调用共 `(P - 1) + P * G` 次，包含初始化改写与每代提案。最终 test 和原生模型/工具轮次另计。

空回复或规范化换行后与父指令相同的回复，会沿用父版本 Snapshot 和分数，不再评估 dev，但仍占据一个种群位置。其他重复指令没有全局缓存，因此可能再次产生评估开销。

## 查看实验记录

每条 `generation` 决策记录 `variant`、`step`、提出的 `candidates` 和幸存的 `population`。成员行包含指令文本、分数和 Snapshot ID。`carried: true` 表示沿用父版本结果，不是新评估后得到同分。

最终 `promote` 记录提供所选分数、基线分数及最终种群。在 standard GA 下，选中的全局最佳可能不在最后的活动种群中。应读取提升记录选中的 Snapshot，不能假定最后一代必然包含胜者。

## 来源与适配

[实现模块](../flashrsi/algorithms/evoprompt/__init__.py)将 `microsoft/EvoPrompt` 固定在 commit `2e235c9410e38d531dabfae7fdd9cbded1577373`。本地适配通过 FlashRSI Snapshot 和完整 dev 适应度演化一个原生指令文件。[提示模块](../flashrsi/algorithms/evoprompt/_prompts.py)包含初始化、GA 和 DE 模板，回复使用 `<prompt>...</prompt>`。

保留种群和算子机制不等于复现上游模型设置或实验结果。固定大小的种群可能含有重复或沿用的成员，而最终选择会保留跨代最佳 dev 版本。
