# 轻量基准接入：BBEH 与 SuperGPQA

这两个 2025 年发布的基准复用 `QABenchmark`、既有划分器、Agent 文本执行和
runner。Target 每题在独立 Linux 容器中执行，需要 Docker CLI 与运行中的 Linux daemon，容器须能访问模型端点。无需 GPU、搜索服务或 Judge；可使用 MiniAgent
或 Claude Code，算法自身的组件与 Worker 前置条件仍然生效。

| 名称 | 内容与数据规模 | 固定数据来源 | 评分范围 |
| --- | --- | --- | --- |
| `bbeh` | 通用复杂推理，23 类、原始 4,520 题，JSON 约 18.6 MB；示例排除重复题组后为 4,511 题 | [BBEH/bbeh](https://huggingface.co/datasets/BBEH/bbeh/tree/08e07a803851822c04399782ece3c4a07ce419f9) | 上游逐题 correctness，`accuracy` 为逐题均值 |
| `supergpqa` | 研究生知识与推理，26,529 题，原始 JSONL 约 24.0 MB；实际有 4–10 个选项 | [m-a-p/SuperGPQA](https://huggingface.co/datasets/m-a-p/SuperGPQA/tree/4430d4458112c7d4497fdcf94d7cc223313d6acf) | 明确的最终答案字母协议，`accuracy` 为逐题均值 |

数据通过 `hf_hub_download` 获取并复用 Hugging Face 缓存。`limit` 和 `split_counts`
减少执行题数，不减少单个原始文件的下载量。数据不写入源码或 Snapshot。

## 运行

复制一个示例到项目根目录，填写目标 Agent 和 Worker 各自的模型与凭据：

```powershell
Copy-Item examples/bbeh_opro.yaml bbeh.local.yaml
python scripts/run_experiment.py --config bbeh.local.yaml --plan
python scripts/run_experiment.py --config bbeh.local.yaml --check-only
python scripts/run_experiment.py --config bbeh.local.yaml
```

SuperGPQA 对应 `examples/supergpqa_opro.yaml`，同样运行。两个示例均使用
OPRO、一轮、一个候选，划分为 8 train / 4 dev / 4 test。
`--check-only` 会加载数据和检查算法初始化，不调用模型，不验证 API 认证。
若默认缓存目录不可写，一键入口可通过 `--cache-dir` 指定缓存位置；直接调用已安装的
`flashrsi` 时可设置 `HF_HOME`（已有 `HF_HUB_CACHE` 优先级更高）。

## 数据划分与本地格式

两个上游都只有公开评估题池，没有独立原生 train/dev/test 三分集。
适配器将原始题池标为 `test`，避免单文件被 HF 推断成 `train`。
`split: auto` 保留该角色，train/dev 为空；需要训练数据的演化算法会明确拒绝。

示例中的 `split_counts` 是对公开题池做的**自定义实验划分**，不代表官方榜单 test 成绩。
划分遵循已有 seed 与 ID 排序规则；三个集合互斥，同配置可重现。测试题只由现有
runner 的 sealed test 流程使用。不得把演化用过的题再称为未见过的官方评估集。

**BBEH 原始数据问题已实测确认：** 两组重复题目涉及 9 条记录，其中 Linguini
7 条相同题目有不同答案，Spatial Reasoning 2 条完全重复。默认
`duplicate_questions: error` 明确报错；示例显式设置 `duplicate_questions: exclude`，
在划分前排除整个重复题组，剩余 4,511 题。排除数量、题目 ID 和策略写入 benchmark
metadata，并输出日志。不会任选一个冲突答案，也不会通过给重复题加不同 ID 来绕过检查。
这是有记录的过滤实验池，不是完整的原始 BBEH 评测。

离线运行可以使用未改字段的原始 JSON / JSONL / Parquet / CSV，以及已有路径映射：

```yaml
benchmark:
  name: supergpqa
  data_path:
    train: ./data/train.jsonl
    dev: ./data/dev.jsonl
    test: ./data/test.jsonl
  split: auto
```

路径相对 YAML 解析；也可用单文件配合 `split_counts`。
BBEH 记录要求 `task`、`input`、`target`；保留原始题目文本，以题型和题目内容生成稳定 ID，
不把 `target` 放进题目。SuperGPQA 要求 `uuid`、`question`、`options`、`answer_letter`、
`answer`，验证答案字母与选项文本一致；题目只包含问题、选项和输出格式要求。
重复 ID 在划分时拒绝，失败轨迹得零分，评分使用已加载的数据答案。

## 评分协议与已接入边界

BBEH 使用 [Google DeepMind 原始评分器](https://github.com/google-deepmind/bbeh/blob/80d12ca916b7158f22293fcf3144f4d3d854d4be/bbeh/evaluate.py)，
只移除导入时的演示打印并格式化代码。`scoring: official` 保留其答案提取、大小写、
数值和括号等匹配行为。框架报告所选题目的逐题均值，不额外生成分题型榜单或类别宏平均。
当前加载 full 题池，没有实现原生 mini 子集的选择开关。

参考答案回填检查为 4,500 / 4,511：剩余 11 条 Linguini 答案以句号结尾，上游评分器
仅去掉预测中的句号而保留参考答案句号，导致原样回填也判错。这一上游行为保留并记录，
没有静默修补答案或评分器。适配内评分函数与固定 commit 的原始函数做过 AST 一致性校验。

SuperGPQA 使用 `scoring: answer_letter`，不声称复现上游完整排行榜协议。
模型可以先推理，但最后一个非空行须是一个答案字母，例如 `I`；也接受小写、
`Answer: I` 和末尾句号。正文中提及正确选项、多字母列举、选项文本或 `\boxed{I}`
都不会自动计对。不会调用 Judge 猜测答案，也不随机补选。
上游的宽松自由文本/选项内容解析、原生 five-shot 提示和学科宏平均暂未接入；
配置 `scoring: official` 会报错，避免把另一种协议误标为官方分数。
实现是独立的简短字母解析器，没有引入上游的报表、推理后端和 POSIX 正则超时依赖。

## 来源与验证

BBEH：[论文](https://arxiv.org/abs/2502.19187)、[上游仓库](https://github.com/google-deepmind/bbeh)。

SuperGPQA：[论文](https://arxiv.org/abs/2502.14739)、[上游仓库](https://github.com/SuperGPQA/SuperGPQA)。
数据集为 ODC-BY，组成内容保留各自的许可与署名要求。运行数据没有随包分发，
研究引用应同时遵循上游 README 的来源说明。

数据获取、schema 与参考答案自检、离线测试和 wheel 入口检查各自验证不同环节，
均不替代算法 × Target Agent × Benchmark 的真实模型运行。真实模型演化效果仍需
在所选配置下单独验证。
