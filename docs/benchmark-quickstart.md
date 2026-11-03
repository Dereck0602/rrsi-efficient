# Benchmark 配置索引

当前注册 **30 个 Benchmark、13 个算法、7 个 Target Agent**。同一适配器的版本、年份和领域不重复计数；例如 Terminal-Bench 2.0 / 2.1、AIME 2025 / 2026、τ² 三个领域分别只计一项。

其中 29 项可选择 [`RUC-NLPIR/FlashRSI` 的 HF prepared 数据源](hf-prepared-data.md)，共 34 份配置，使用固定发布划分和校验过的原生资源；原有默认来源保持不变。

从项目根目录用 `python scripts/run_experiment.py --config examples/<示例>.yaml --plan` 查看所选依赖，再填配置运行 `--check-only`，最后去掉该参数开始实验。两个模型角色及 Judge / User Simulator / Postprocess 互不继承凭据。示例包含不同算法，切换时也需保留对应组件配置。

注册、离线接口测试、真实 smoke 是不同状态。Smoke 结果和验证报告仅本地保存，不随源码发布；Codex 此次合入不表示 30 项均已用它完成真实运行。完整入口说明见[一键实验指南](experiment-launcher.zh-CN.md)。

所有接入遵循同一条边界：演化的 harness 由 Snapshot 固定，目标任务的数据、环境和领域工具由 benchmark 接入层提供。
SearchQA、MuSiQue、TriviaQA、NQ、2WikiMultiHopQA、MATH、AIME、BBEH、SuperGPQA，以及 HotpotQA 的 `context` 模式需要 Docker CLI 和 Linux daemon。每题的 Target 在独立容器内执行，宿主机保留标签和评分记录。容器需要能访问配置的模型端点。边界及 Python API 用法见下方链接。
逐项分工与验证范围见 [Harness 与任务环境边界](benchmark-harness-boundary.md)；迁移评估入口见 [Snapshot 评估](snapshot-evaluation.md)。

| 领域 | Registry ID | 完整示例 | 额外准备 / 接入边界 |
| --- | --- | --- | --- |
| Working / Tool Use | `appworld` | [modular_rsi_appworld.yaml](../examples/modular_rsi_appworld.yaml) | 启动器自动准备原生数据及默认 Docker 镜像；显式 `data_path` 只使用已有本地数据 |
| Working / Tool Use | `workbench` | [workbench_opro.yaml](../examples/workbench_opro.yaml) | 原生数据库；`all` extra 提供 NumPy 1.x |
| Working / Tool Use | `bfcl` | [bfcl_opro.yaml](../examples/bfcl_opro.yaml) | Docker；Multi-Turn Base 范围 |
| Working / Tool Use | `harvey_lab` | [harvey_lab_opro.yaml](../examples/harvey_lab_opro.yaml) | 法律文档语料；独立 Anthropic / OpenAI Judge |
| Working / Tool Use | `officeqa_pro` | [officeqa_pro_opro.yaml](../examples/officeqa_pro_opro.yaml) | 题目和本地文档语料；文件工具 |
| Working / Tool Use | `jobbench` | [jobbench_opro.yaml](../examples/jobbench_opro.yaml) | Docker + Harbor；独立 Judge 配置 |
| Working / Tool Use | `travelplanner` | [travelplanner_opro.yaml](../examples/travelplanner_opro.yaml) | sole-planning；原生数据库及独立 postprocess 模型 |
| Working / Tool Use | `tau2` | [tau2_opro.yaml](../examples/tau2_opro.yaml) | 固定 tau2 环境、用户模拟器模型；与 Harbor 分环境 |
| Working / Tool Use | `tau3_banking` | [tau3_banking_opro.yaml](../examples/tau3_banking_opro.yaml) | 固定 tau2 环境、BM25 数据、用户模拟器模型 |
| Working / Tool Use | `spreadsheetbench2` | [spreadsheetbench2_opro.yaml](../examples/spreadsheetbench2_opro.yaml) | Docker + Harbor + LibreOffice；只接前三类，不含 Visualization |
| Search / QA | `searchqa` | [searchqa_skillopt.yaml](../examples/searchqa_skillopt.yaml) | 官方数据首次下载较大；可先跑附带 12 题 quickstart |
| Search / QA | `hotpotqa` | [hotpotqa_opro.yaml](../examples/hotpotqa_opro.yaml) | 本地或原生数据；按示例设置实验划分 |
| Search / QA | `twowiki_multihopqa` | [twowiki_multihopqa_opro.yaml](../examples/twowiki_multihopqa_opro.yaml) | 本地或原生数据；按示例设置实验划分 |
| Search / QA | `musique` | [modular_rsi_musique.yaml](../examples/modular_rsi_musique.yaml) | 本地或原生数据；按示例设置实验划分 |
| Search / QA | `triviaqa` | [modular_rsi_triviaqa.yaml](../examples/modular_rsi_triviaqa.yaml) | 本地或原生数据；按示例设置实验划分 |
| Search / QA | `nq` | [nq_opro.yaml](../examples/nq_opro.yaml) | 本地或原生数据；按示例设置实验划分 |
| Search / QA | `browsecomp` | [browsecomp_serper_jina.yaml](../examples/browsecomp_serper_jina.yaml) | 搜索服务、页面抓取及独立 Judge |
| Search / QA | `deepsearchqa` | [deepsearchqa_serper_jina.yaml](../examples/deepsearchqa_serper_jina.yaml) | 搜索服务、页面抓取及独立 autorater |
| Search / QA | `widesearch` | [widesearch_opro.yaml](../examples/widesearch_opro.yaml) | 搜索服务、页面抓取、Judge；使用 `all` extra 的共享 NumPy 1.x |
| Coding | `swe_bench` | [swe_bench_verified_opro.yaml](../examples/swe_bench_verified_opro.yaml) | 独立原生 SWE-bench Docker verifier，不走 Harbor |
| Coding | `swe_bench_pro` | [swe_bench_pro_opro.yaml](../examples/swe_bench_pro_opro.yaml) | Docker + Harbor；独立原生 verifier |
| Coding | `terminal_bench` | [terminal_bench21_opro.yaml](../examples/terminal_bench21_opro.yaml) | Docker + Harbor；支持 2.0 / 2.1 |
| Coding | `deepswe` | [deepswe_opro.yaml](../examples/deepswe_opro.yaml) | Docker + Harbor；接口适配，未下载官方任务或开展实验 |
| Knowledge / Reasoning | `bbeh` | [bbeh_opro.yaml](../examples/bbeh_opro.yaml) | 本地或原生数据；按示例设置实验划分 |
| Knowledge / Reasoning | `supergpqa` | [supergpqa_opro.yaml](../examples/supergpqa_opro.yaml) | 本地或原生数据；按示例设置实验划分 |
| Knowledge / Reasoning | `hle` | [hle_opro.yaml](../examples/hle_opro.yaml) | 官方 HF `cais/hle`；固定版本的纯文本子集，自动排除带图题；HF 授权及独立 Judge |
| Math | `math` | [modular_rsi_math500.yaml](../examples/modular_rsi_math500.yaml) | 本地或原生数据；按示例设置实验划分 |
| Math | `aime` | [aime_opro.yaml](../examples/aime_opro.yaml) | 本地或原生数据；按示例设置实验划分 |
| Math | `livemathbench` | [livemathbench_opro.yaml](../examples/livemathbench_opro.yaml) | 独立 Judge；按示例设置版本及采样次数 |
| Embodied / Interactive | `alfworld` | [alfworld_opro.yaml](../examples/alfworld_opro.yaml) | 纯文本；预先准备原生数据；Linux/WSL + Python 3.11/3.12 |

共 30 项。ALFWorld 单列为交互环境，避免把它混同办公任务。

Docker / Harbor 的资源准备细节见 [Coding / Working 指南](coding-working-benchmarks.md)、[DeepSWE](deepswe.md)、[ALFWorld](alfworld.md) 和 [Web 服务](web-research-services.md)。
