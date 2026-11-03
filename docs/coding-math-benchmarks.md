# AIME 接入

AIME 适配器复用 `QABenchmark`、数据划分、Agent 文本执行和 runner。
Target 每题在独立 Linux 容器中执行，需要 Docker CLI 与运行中的 Linux daemon，容器须能访问模型端点。无需 GPU 或 Judge。算法与 Target Agent 的组件要求仍由
初始化检查负责。

| 配置 | 内容 | 固定数据来源 | 评分 |
| --- | --- | --- | --- |
| `name: aime`, `year: 2026`（默认） | 2026 AIME 数学题，30 题 | [math-ai/aime26](https://huggingface.co/datasets/math-ai/aime26/tree/79037aebdb6580008fb960d17cb21fd3099083e3) | `integer_exact`，最终整数准确率 |
| `name: aime`, `year: 2025` | 2025 AIME 数学题，30 题 | [math-ai/aime25](https://huggingface.co/datasets/math-ai/aime25/tree/563bb8404243c5f09de6ec262f2db674fe5bce9b) | 同上 |

下载使用 Hugging Face 缓存并固定 revision。数据不随 wheel 分发，也不进入 Snapshot。

## 运行

复制示例到项目根目录，填写目标 Agent 与 Worker 各自的模型、连接和凭据：

```bash
cp examples/aime_opro.yaml aime.local.yaml
python scripts/run_experiment.py --config aime.local.yaml --plan
python scripts/run_experiment.py --config aime.local.yaml --check-only
python scripts/run_experiment.py --config aime.local.yaml
```

切换到 2025 年可追加 `--benchmark.year 2025`。
示例为 OPRO、一轮、一个候选、8 train / 4 dev / 4 test；AIME 总题量小，这个
划分用于验证链路，不足以支持稳定的泛化结论。
`--check-only` 加载真实数据并初始化组件，不调用模型、不验证 API 认证。

一键入口可用 `--cache-dir` 指定可写缓存；直接运行已安装的 `flashrsi` 时可设置
`HF_HOME`（已有 `HF_HUB_CACHE` 优先）。`limit` / `split_counts` 只减少实验题数，
仍会下载完整原始文件。

## 数据与划分

两个年份都只有原生公开 `test`。默认 `split: auto` 保留这一角色，train/dev 为空；
需要训练数据的算法会报错。示例显式将公开题池划分为互斥的实验 train/dev/test，
使用既有 seed 与任务 ID 规则；这是**自定义实验划分**，不能报告成官方隐藏测试成绩。
最终测试沿用 runner 的 sealed test 流程。

本地数据保持原字段即可，支持 JSON / JSONL / Parquet / CSV 和路径映射：

```yaml
benchmark:
  name: aime
  year: 2026
  data_path:
    train: ./data/train.jsonl
    dev: ./data/dev.jsonl
    test: ./data/test.jsonl
```

路径相对配置文件解析。也可用单文件加 `split_counts`；任意文件名若没有明确角色，
不会自动作为训练数据。

AIME 要求 `id`、`problem`、`answer`。原始 2025 ID 为字符串、2026 ID 为整数，
均接受；答案须为 0–999 的整数或 1–3 位数字字符串。任务 ID 带年份前缀。
本地数据的年份由 `benchmark.year` 指定，题目文本原样保留，仅追加答案格式要求。
重复 ID 会在划分时拒绝；失败轨迹计零分；评分依据加载时保存的答案，
不会使用轨迹里被替换的参考答案。

## 评分协议

AIME 使用 `scoring: integer_exact`。先读取最后一个 `\boxed{...}` 或
`\fbox{...}`；没有框时读取最后一个非空行。只接受 1–3 位十进制数字，前导零等价。
末尾不完整的框计错，不回退到前面的正确答案。整数之外的算式、浮点数、选项或
含说明文字的答案行不做猜测，也不调用符号数学或 Judge。指标 `accuracy` 是逐题均值。

数据来自上述 math-ai 镜像；该适配器使用显式的最终整数评分协议。
参考答案回填可检查加载和评分链路，不能代替模型解题实验或数学正确性验证。

## 独立演化来源

`benchmark.evolution_year` 可以显式选择另一个已接入的年份。例如，AIME2026 的实验使用
`evolution_year: 2025`、`evolution_limit: 1`。这将另一年的公开 test 题池用于自定义演化实验。
两个完整题池先检查题目文本重合，之后才抽取演化题；2026 的 dev/test 划分保持原样，
train 替换为 2025 题目。原生整数评分不变，元数据记录来源、revision、年份、题池校验和
与用途；手填 `evolution_source` 不能绕过检查。

见 [ModularRSI + AIME 示例](../examples/modular_rsi_aime.yaml)。Claude Code 版本使用
[三扩展示例](../examples/modular_rsi_claude_code.yaml) 中的 `agent.source_dir` 与
prompt/skill/hook 文件映射，并保留各角色独立的模型配置与预算。

执行型 Coding 基准见 [Coding 与 SpreadsheetBench 2 接入](coding-working-benchmarks.md)。
