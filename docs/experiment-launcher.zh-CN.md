# 一键实验入口与接入说明

[返回首页](../README.zh-CN.md) · [使用指南](usage.zh-CN.md)

FlashRSI 用同一套实验入口连接 Algorithm、Benchmark 和 Agent，记录演化版本、逐题轨迹与最终 test 结果。
当前注册 13 个算法、7 个 Target Agent、30 个 Benchmark；接入范围及各项额外配置见
[Benchmark 配置索引](benchmark-quickstart.md)。注册数量不表示所有组合均已通过真实运行。

首次体验也可使用附带 12 道题的 [quickstart_launch.yaml](../examples/quickstart_launch.yaml)，无需下载 SearchQA 全量数据。Codex 的额外工具链和 Responses API 要求见 [Codex 指南](codex.md)。

## 第一次运行

以下命令针对下载或克隆后的完整源码目录，需要包含 `scripts/`、`examples/` 和 `flashrsi/`。
推荐在 **Linux / WSL + Python 3.12** 下开始；这覆盖当前主要依赖的共同版本范围。
Python 3.11 可运行部分轻量基准，但 Harbor、WorkBench、τ 系列等需要更新版本；
ALFWorld 仅支持本接入声明的 Python 3.11/3.12。不要把“Python ≥3.11”理解为所有版本适用于所有基准。
WSL 中以下命令应在 WSL 终端执行。MiniAgent 自带的 shell 依赖 POSIX；
Windows 原生不作为所有组合的通用运行环境。

1. 进入项目根目录，确认 `python --version` 为所选版本、`python -m pip --version` 可用。
   Linux 需提供 `venv`/`ensurepip`（发行版可能需要安装 `python3-venv`）；
   使用容器基准时还需 Git、Docker Linux daemon，Harbor 类另需 Docker Compose。
   默认 SearchQA 首次会下载约 3.17 GB 原始数据；抽取少量题目不会减少下载量。
2. 执行下面的命令生成本地配置。第一次会正常提示配置位置并退出（退出码 2），此时尚未安装实验依赖。

```bash
python scripts/run_experiment.py
```

3. 编辑 `experiment.local.yaml`，分别填写 `agent.llm` 和 `algorithm.worker.llm` 的
   `model_name`、`api_key`、`base_url`。两者互不继承；同一服务也要分别配置。
   使用自定义模型网关时填写它对应的 API 地址；使用 OpenAI 官方服务时地址可以留空。
   也可以使用下文的 `FLASHRSI_AGENT_*` / `FLASHRSI_WORKER_*` 环境变量。
4. 依次查看计划、检查准备状态、运行实验：

```bash
python scripts/run_experiment.py --config experiment.local.yaml --plan
python scripts/run_experiment.py --config experiment.local.yaml --check-only
python scripts/run_experiment.py --config experiment.local.yaml
```

`--plan` 应输出选中的算法、Agent、Benchmark、依赖和前置条件；`--check-only` 成功时输出
`Ready: train=..., dev=..., test=...`，但不测试远程模型是否可用。最后一条命令才开始调用模型，
成功时输出 `Completed experiment: ...` 与 test 分数。默认实验为小规模 SearchQA + MiniAgent + SkillOpt，
完成后按[查看结果](#查看结果)检查产物。

首次无需手工激活项目 `.venv`。一键脚本会创建独立环境并安装 `all` 依赖；Harbor 基准会额外安装
`harbor` extra；
`python -m flashrsi` 是已安装环境入口，不会自动安装依赖或执行相同的资源准备流程。

## 更换实验组合

入口根据 YAML 中的选择自动创建 Python 环境、安装依赖、准备对应原生数据，
再调用现有 runner 完成演化、test 评估和结果保存。更换算法不需要修改脚本：

```yaml
benchmark:
  name: searchqa
agent:
  name: miniagent
algorithm:
  name: skillopt
  worker:
    type: llm
```

这是选择字段示意；请在完整配置中修改，并保留各角色的模型配置。
不同算法的参数各自生效，切换时应同步调整，例如 OPRO 使用 `max_steps`、`num_candidates`，
SkillOpt 使用 `num_epochs`、`batch_size` 等。AHE 必须使用 Agent Worker。

| 选择字段 | 可用值 |
| --- | --- |
| `algorithm.name` | `ace`、`adas`、`ahe`、`darwinx`、`evo_harness`、`evoprompt`、`gepa`、`meta_harness`、`modular_rsi`、`opro`、`rrsi`、`skillopt` |
| `agent.name` | `miniagent`、`claude_code`、`codex`、`pi`、`hermes`、`openclaw`、`opencode` |
| `algorithm.worker.type` | `llm`；或 `agent` 并配置 `worker.name: miniagent / claude_code / codex / pi / hermes / openclaw / opencode` |
| `benchmark.name` | [30 个注册项及对应示例](benchmark-quickstart.md) |

`adas`、`darwinx`、`evo_harness`、`meta_harness`、`modular_rsi`、`rrsi` 和 `ahe`
需要能编辑文件的 Worker：设置 `algorithm.worker.type: agent`，并填写 `worker.name`
及其模型配置。入口会在安装依赖前检查 Worker 类型。新算法的完整配置见 `examples/` 同名 YAML，
具体约束见 [ADAS](adas.md)、[DarwinX](darwinx.md)、[EvoHarness](evo-harness.md)、
[MetaHarness](meta-harness.md)、[ModularRSI](modular-rsi.md)、[RRSI](rrsi.md)。
ModularRSI 使用当前 Benchmark 的 `train` 演化，允许与 dev/test 来自同一数据集；
train 任务 ID 必须唯一且与 dev/test 不重合。需要配置模块文件映射；可选的独立演化数据
仍可通过 `benchmark.evolution_data_path` 等现有参数提供，见 [SWE 示例](../examples/modular_rsi_swe_bench.yaml)。默认采用五个运行时模块，
Claude Code 可使用 [prompt / skill / hook 三扩展示例](../examples/modular_rsi_claude_code.yaml)。

上述六个新增算法来自 FlashRSI-Lite commit `d8d5e35e77e1c0360a1df55505db3cfb2b113cd0`，
已有六个算法保留当前项目的实现与修复。

例如，把 [HotpotQA + OPRO 示例](../examples/hotpotqa_opro.yaml)复制为同目录下的
`examples/hotpotqa.local.yaml`，填写模型配置后运行：

```bash
cp examples/hotpotqa_opro.yaml examples/hotpotqa.local.yaml
python scripts/run_experiment.py --config examples/hotpotqa.local.yaml --plan
python scripts/run_experiment.py --config examples/hotpotqa.local.yaml --check-only
python scripts/run_experiment.py --config examples/hotpotqa.local.yaml
```

PowerShell 可用 `Copy-Item` 替代 `cp`。保留示例所在目录可以维持相对路径语义；
若移动 YAML，必须同步调整数据路径、输出目录以及 Agent/Worker 的 `source_dir`。
30 个 Benchmark 均有可定位的完整示例，见[配置索引](benchmark-quickstart.md)。

切换 Claude Code 时，将 `agent.name` 改为 `claude_code`，并填写该角色的 Claude 模型和
认证配置。Agent Worker 的配置形如：

```yaml
worker:
  type: agent
  name: claude_code
  llm:
    model_name: YOUR_CLAUDE_MODEL
    api_key: YOUR_API_KEY
    base_url: YOUR_ANTHROPIC_COMPATIBLE_URL
```

Claude Code 可用 `llm.auth: login` 复用本机已有登录；容器任务需要显式凭据。
需要 shell 执行能力的基准还需配置 `agent.allowed_tools: [Read, Glob, Grep, Bash]`；
更换 Agent Worker 时同样检查其文件编辑权限和算法所需组件，不能只替换模型名。
入口会安装 Claude Agent SDK。算法的组件要求和 Benchmark 的执行能力仍由现有实现校验，
并不保证任意参数、认证方式和环境组合都兼容。

## 准备与检查

```powershell
# 查看所选依赖和前置条件；首次只建立读取 YAML 的轻量环境
python scripts/run_experiment.py --config experiment.local.yaml --plan

# 安装所选依赖、准备数据和必要镜像、检查实例化；不调用模型
python scripts/run_experiment.py --config experiment.local.yaml --check-only

# 指定缓存盘
python scripts/run_experiment.py --config experiment.local.yaml --cache-dir D:\FlashRSI-cache
```

`--plan` 需要合法的配置和模型名，但不检查 API 密钥、不下载实验数据。
`--check-only` 会实际下载数据，必要时构建或拉取镜像；它不验证远程 API 认证和模型可用性，
不创建实验结果目录。正常运行会调用配置中的模型。

| Benchmark | 自动准备 | 用户需要提供的额外条件 |
| --- | --- | --- |
| `bbeh`、`supergpqa` | 固定版本公开数据，本地确定性评分，无新增依赖 | 显式划分实验 train/dev/test；BBEH 示例显式排除重复题组，SuperGPQA 使用答案字母协议，详见[轻量基准接入](lightweight-benchmarks.md) |
| `aime` | AIME 2025/2026 数学题，无新增依赖 | 整数精确评分；详见[AIME 接入与历史记录](coding-math-benchmarks.md) |
| `twowiki_multihopqa` | 固定版本 HF 镜像的 train/validation；也支持本地原始 JSON | 仅 context + answer_text（答案 EM/F1），不含 v1.1 别名、证据链或联合评分；显式划分实验 train/dev/test |
| `searchqa`、`hotpotqa`、`musique`、`triviaqa`、`nq` | 固定来源的原生 QA 数据 | HotpotQA `fullwiki` 模式另需部署检索服务并配置 `benchmark.retrieval` |
| `math` | 原生数据、数学评分依赖 | 原生答案须通过评分器校验；异常记录会明确报错 |
| `hle`、`livemathbench` | 原生数据、Judge 依赖 | 上游要求授权时提供获批的 `HF_TOKEN`，或使用本地数据；配置独立 Judge |
| `officeqa_pro` | 原生题目下载/本地加载 | 有权访问的数据及 `benchmark.corpus_path` 文档语料 |
| `alfworld` | 固定纯文本运行依赖；任务只从本地加载 | Linux/WSL/macOS、Python 3.11/3.12、原生 `benchmark.data_path`；无需 Docker/Judge，见 [接入说明](alfworld.md) |
| `workbench` | 固定版本原生数据库、v2 任务和状态评分，自动准备依赖 | Python ≥3.12、Git，无需 Docker/Judge；显式实验划分，见 [接入说明](workbench.md) |
| `travelplanner` | 原生数据、参考数据库、执行依赖 | 独立 `benchmark.postprocess.llm` |
| `harvey_lab` | 固定 LAB 依赖、原生数据、Pandoc 二进制 | Python 3.12–3.13、Git、独立 Anthropic/OpenAI Judge；默认 MiniAgent shell 需 POSIX 环境 |
| `browsecomp`、`deepsearchqa`、`widesearch` | 原生数据、各自评分依赖 | 搜索/网页服务及独立 Judge；DeepSearchQA 使用原生指定 Judge 模型 |
| `swe_bench` | 原生数据、SWE-bench harness；任务镜像交给原生运行时准备 | Git、可用的 Docker Linux daemon |
| `swe_bench_pro`、`terminal_bench`、`deepswe`、`jobbench`、`spreadsheetbench2` | Harbor 依赖及原生数据；任务镜像交给原生运行时准备 | Python ≥3.12、Docker Linux daemon + Compose、Git；JobBench 另需独立 `benchmark.judge.llm` |
| `appworld` | 构建默认运行镜像、下载并缓存原生数据 | Docker Linux daemon |
| `bfcl` | 构建默认运行镜像、获取固定版本原生数据 | Docker Linux daemon、自动获取数据需 Git |
| `tau2` | 固定版本 τ² 文本任务：airline、retail、telecom；原生模拟器、工具与评分 | Python 3.12–3.13、Git、独立用户模拟器；retail 另需 Judge，无需 Docker；见 [τ² 接入说明](tau2-bench.md) |
| `tau3_banking` | 固定版本 tau2、原生银行任务和用户模拟器资源 | Git、独立 `benchmark.user_simulator.llm`；硬超时需 POSIX |

Terminal 2.1、SWE-bench Verified / Pro 和 SpreadsheetBench 2 的版本、分类与示例见
[Coding 与 Working 接入说明](coding-working-benchmarks.md)。DeepSWE v1.1 的接口与配置见[接入说明](deepswe.md)。CRUXEval-O 已移除，旧 smoke 仅作历史记录。

系统级 Git、Docker 及 daemon 需预先安装/启动，入口会检查前置条件。
Linux 的 Python 需要可创建带 pip 的 venv（发行版拆包时需安装相应 `python3-venv` 包）。
Windows 请将输出目录放在较短路径，或启用系统长路径支持；Snapshot 文件名和实验目录
叠加后可能超过旧版 Windows 路径限制，网络和模型正常也不能避免该文件写入错误。
MiniAgent 自带的本机 shell 工具依赖 POSIX（包括 `/bin/sh`）；需要这项能力的任务或
Agent Worker 应在 Linux/WSL 中运行。
AppWorld/BFCL 配置自定义 `benchmark.image` 时，优先复用本地镜像，否则拉取该标签。
AppWorld/BFCL 的 Agent 容器默认从 Astral 安装固定版本的 uv；若该域名不可达，可设置
`agent.container_uv_install_source: github`，改用同版本的官方 GitHub 安装脚本。
运行镜像若已在 `/opt/flashrsi-lite/bin/uv` 安装 uv 0.11.4，启动器会复用它；版本不匹配时仍按所选来源安装。
平台使用非默认 Docker socket 时，在启动命令的环境中设置 `DOCKER_HOST=unix:///实际路径/docker.sock`。
显式设置 `benchmark.data_path` 时使用本地数据；各 Benchmark 原生加载器继续负责格式、
版本和完整性校验。已有 `TAU2_DATA_DIR` 会保留，缺失时自动准备匹配版本的 tau2 资源。

`benchmark.split: auto` 保留原生 train/dev/test，不会为了凑齐集合而暗中重新划分。
没有原生三分集的数据，应显式指定 `split_counts`、`split` 比例，或 `data_path` 的
train/dev/test 路径映射。HotpotQA 示例从有标签的原生 train/validation 池中做自定义划分，
不是官方测试集评测。数据、角色配置及算法条件不满足时会报错。

## 模型配置与文件位置

各角色独立配置，不隐式继承密钥。环境变量覆盖 YAML 中的值：

| 环境变量前缀 | 对应配置 |
| --- | --- |
| `FLASHRSI_AGENT_` | `agent.llm` |
| `FLASHRSI_WORKER_` | `algorithm.worker.llm` |
| `FLASHRSI_JUDGE_` | `benchmark.judge.llm` |
| `FLASHRSI_USER_SIMULATOR_` | `benchmark.user_simulator.llm` |
| `FLASHRSI_POSTPROCESS_` | `benchmark.postprocess.llm` |
| `FLASHRSI_JUDGE_ANTHROPIC_` / `FLASHRSI_JUDGE_OPENAI_` | `benchmark.judge.anthropic` / `benchmark.judge.openai` |

每个前缀可接 `MODEL`、`API_KEY`、`BASE_URL`，例如 `FLASHRSI_WORKER_API_KEY`。
纯 LLM Worker 使用 Chat Completions；MiniAgent 对 `gpt-`、`o1`、`o3`、`o4` 开头的模型
使用 Responses API，其他模型使用 Chat Completions，提供商应支持对应接口。
OpenAI 官方接口的 `base_url` 可留空。

默认缓存位于 Windows `%LOCALAPPDATA%\FlashRSI` 或 Linux/macOS
`${XDG_CACHE_HOME:-~/.cache}/FlashRSI`。依赖按组合隔离，避免 tau2 与 Harbor 的依赖冲突；
相同组合复用环境，源码或依赖声明变化时重新安装。依赖遵循 `pyproject.toml` 中的版本范围，
不使用 `uv.lock` 锁定全部传递依赖。已有 `HF_HOME`、`HF_HUB_CACHE`、`PIP_CACHE_DIR` 继续生效，
Docker 镜像由 Docker 管理，不受 `--cache-dir` 控制。

YAML 中的 `run.output_dir`、数据路径（含 `evolution_data_path`）、语料路径、Agent/Worker 的 `source_dir` 与
`cli_path` 相对 YAML 所在目录解析。默认根目录配置将结果写入 `runs/`，配置和记录脱敏保存。
`*.local.yaml`、缓存、构建产物和实验目录已加入 Git 忽略规则。
失败后重跑会复用依赖和下载，但实验新建目录，不自动续跑中断的演化。

默认 SearchQA 示例在原生各划分内共抽取 30 题，运行一次 fast update 后评估 test。
它用于验证流程；更多演化需调整预算，至少两轮才可能触发 SkillOpt slow update。
`limit: null` 使用全量。**抽样不减少原始数据下载量**：SearchQA 三个 ZIP 共约 3.17 GB。
下载只存用户缓存，不带入源码或 Snapshot。

原来的 `python scripts/run_searchqa.py` 命令仍兼容，默认读取 `searchqa.local.yaml`，
内部转到通用入口。普通 `python -m flashrsi run --config ...` CLI 仍按已安装环境运行。

2WikiMultiHopQA 示例：`examples/twowiki_multihopqa_opro.yaml`。默认数据源为
`framolfese/2WikiMultihopQA`（固定 revision），原始来源为
[Alab-NII/2wikimultihop](https://github.com/Alab-NII/2wikimultihop)。
示例将有标签的 train/validation 池重新划分为实验集，不是官方 test 成绩。
本地原始数据建议显式指定 `benchmark.data_path: {train: /path/train.json, dev: /path/dev.json}`，
避免把无标签 test 或 id_aliases.json 当作问答数据加载。

## 查看结果

每次运行在 `run.output_dir` 下新建 `{algorithm}-{agent}-{benchmark}-{time}` 目录，不覆盖旧实验。
默认首次生成的根目录配置写入项目 `runs/`；其他 YAML 按自身所在目录解析输出路径。

| 文件或目录 | 用途 |
| --- | --- |
| `end-summary.md` / `end-summary.json` | 先看运行总结、最终 test 指标及已记录的 Target/Worker 用量；SDK 费用不等于完整供应商账单 |
| `result.json` | 成功执行后的最终 Snapshot ID、主指标、test 题数、Benchmark 来源信息 |
| `error.json` | 运行失败时的错误记录；启动前校验失败只在终端报错，可能尚未创建实验目录 |
| `config.yaml` | 本次解析后的配置，保存时凭据脱敏；复跑须重新提供凭据并核对相对路径 |
| `snapshots/` | 初始、候选及最终选用的版本记录 |
| `trajectories/` | 逐批任务、模型输出、指标、执行状态及用量 |
| `trajectories/*-<label>.json` | 算法的候选、评估、选择等事件 |
| `working/` | 算法的工作文件；不同算法可能另有专属记录目录 |
| `artifacts/harbor/trial-*/` | Harbor 已收集的交付文件、原生评分明细与日志；每次执行单独保存，包含归档清单 `manifest.json` |

`Completed experiment` 表示框架流程结束，不保证分数提高或所有任务执行成功。
先核对 `test_tasks > 0`，再查看轨迹中的 `status` 和错误；模型预算耗尽、Judge 失败与正常零分需要区分。
示例多为小规模流程验证，修改 `split_counts`、`limit` 和算法预算后才是你自己的实验设置。
Terminal、DeepSWE、JobBench、SpreadsheetBench 2 和 SWE-bench Pro 在临时 trial 清理前归档
Harbor 已收集的文件。轨迹的 verifier 事件记录归档目录和文件清单；SWE-bench Pro 分别记录
`solve_artifacts` 与 `regrade_artifacts`。失败或正常取消时也保留已经落盘的文件；
强制终止进程或主机故障不保证执行归档。归档失败会明确标记执行错误。

SpreadsheetBench 2 的工作簿一般位于归档中的 `artifacts/logs/artifacts/`，
原生明细位于 `verifier/native-result.json`。交付文件和补丁保留原始字节，UTF-8 日志及清单替换已知凭据；
不复制包含连接凭据的 trial 配置、lock 或 payload。原始交付文件仍可能含任务数据，按本地实验产物管理。
重新评分仍需要原任务数据、固定版本评分器及运行环境，保存文件不等于已经完成独立重评。

可用 `agent.harbor.artifacts_dir` 显式指定归档根目录，相对路径按启动时工作目录解析；
普通实验默认使用本次实验的 `artifacts/harbor/`。直接调用 Harbor integration 而不经过 runner 时，
默认根目录为当前工作目录的 `runs/artifacts/harbor/`。重复任务或重试均新建目录。

Terminal、DeepSWE、JobBench 和 SpreadsheetBench 2 还支持 `agent.harbor.trials_dir`，
用于额外保留 Harbor 原生 trial 目录；相对路径按启动时工作目录解析，建议使用绝对路径。
设置后，`HarborVerifier` 记录 `trial_directory`、Task checksum、Snapshot ID，以及收集的
产物和 verifier 文件的 SHA256。默认的产物归档仍然执行；未设置时原生 trial 随临时目录清理。
原生 trial 配置可能包含运行凭据，不属于上述脱敏归档，应保留在 Git 忽略的本地实验目录中。
SWE-bench Pro 的双阶段执行仍使用 `solve_artifacts` / `regrade_artifacts`，不支持此选项。

## 常见启动问题

| 现象 | 检查位置 |
| --- | --- |
| 缺少模型名或密钥 | 分别检查 Target、Worker，以及所选基准的 Judge / User Simulator / Postprocess；它们互不继承 |
| 选择 Claude Code 后缺少工具或组件 | 检查 `allowed_tools`、`source_dir` 和算法的模块映射；使用对应的完整 Agent Worker 示例 |
| train/dev/test 为空，或无法映射原生 split | 按示例配置显式实验划分，或提供带角色映射的本地数据；`split: auto` 不会自动凑齐三组 |
| `data_path` / `corpus_path` 不存在 | 以 YAML 所在目录解释相对路径；ALFWorld 数据与 OfficeQA 文档语料须先准备 |
| 模型网关连接失败或不支持协议 | `--check-only` 不验证远程模型；核对模型名、Chat Completions / Responses / Anthropic 协议及认证方式 |
| SDK 超时或达到轮数上限 | 查看失败轨迹，调整对应角色预算后发起新实验；增加预算不保证任务成功 |

## 开发与发布状态

开发者可在独立环境安装所需 extras，再运行 `uv run pytest`、`uv run ruff check .` 和
`uv run ruff format --check .`。含可选原生环境的测试可能跳过，跳过不等于已经验证该能力。
当前接入与历史 smoke 对应不同源码和数据版本，不能把旧版本结果视为所有当前组合的保证。
