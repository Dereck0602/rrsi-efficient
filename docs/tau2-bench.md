# τ²：airline、retail、telecom

`benchmark.name: tau2` 复用 τ³ Banking 的执行桥。目标 Agent 负责客服侧
对话与工具调用；上游用户模拟器、数据库、工具、对话循环和奖励计算照常执行。
telecom 保留用户侧手机工具与环境状态，即论文中的双控制交互。
算法仍通过统一 Benchmark / Agent 接口使用它，无需添加算法分支。

## 版本与题池

[τ² 论文](https://arxiv.org/abs/2506.07982)发表于 2025 年。本接入固定
[sierra-research/tau2-bench v1.0.1](https://github.com/sierra-research/tau2-bench/tree/fc0055dc4e0a316c3f83133267fbd6faaa770992)，
commit `fc0055dc4e0a316c3f83133267fbd6faaa770992`，代码与数据使用同一 revision。
这是项目已有 Banking 接入使用的维护版本；不等同于 2025 论文原始数据快照，
也未接入当前上游 2.0 的多语言、语音能力。上游仓库采用 MIT 许可。

| `benchmark.domain` | 默认 `task_split: base` | 原生 train / test | 原生 reward basis |
| --- | ---: | ---: | --- |
| `airline` | 50 | 30 / 20 | DB、COMMUNICATE |
| `retail` | 114 | 74 / 40 | DB、NL_ASSERTION；其中 40 题含非空自然语言判据 |
| `telecom`（默认） | 114 | 74 / 40 | ENV_ASSERTION；其中 20 题另有 ACTION |

`task_split` 选择上游 `split_tasks.json` 中的题池，`split` / `split_counts` 再决定
FlashRSI 实验划分。默认 `split: auto` 保留原生 train / test，dev 为空；需要 dev 的
算法应显式划分。示例先选择 `task_split: train`，再从该池抽取 4 train / 2 dev /
2 test，因此示例中的 test 是实验留出集，不能报告为官方 test 成绩。
runner 的 sealed test 流程继续生效。

telecom 的 `tasks.json` 共 2,285 条，包括扩展变体；默认只选 base 的 114 条。
可显式使用原生 `small`、`train`、`test`、`full` 等题池。`full` 中额外记录没有原生
train/test 角色，须明确配置实验划分；适配器不会自动把变体加入训练或评测池。

## 运行

使用 Python 3.12 或 3.13，准备 Git 和 Linux Docker daemon。每个 Target 回合只将公开策略、工具说明及对话传入独立容器。原生领域数据库、用户模拟器和评分留在宿主机。

请显式配置 `agent.llm.base_url`。依赖安装结束后，Target 容器会断开默认网络，只通过专用代理访问配置的模型来源（协议、主机和端口）。`agent.llm.side_base_url` 如有配置，也会加入允许列表。解题中的任意公网请求会失败，官方原生工具及其宿主侧服务仍可运行。
统一入口为 τ² 建立包含 `all` extra 的独立依赖环境，并从固定 commit 安装上游 tau2-bench，
避免其 LiteLLM 版本与 Harbor 冲突。

复制示例到项目根目录，填写各角色自己的模型、凭据和连接：

```powershell
Copy-Item examples/tau2_opro.yaml tau2.local.yaml
python scripts/run_experiment.py --config tau2.local.yaml --plan
python scripts/run_experiment.py --config tau2.local.yaml --check-only
python scripts/run_experiment.py --config tau2.local.yaml
```

默认运行 telecom；追加 `--benchmark.domain airline` 可切换航空领域。
retail 使用 `examples/tau2_retail_opro.yaml`，其中包含独立 Judge 配置。
示例采用 OPRO、一轮、一个候选。`--check-only` 仅准备并初始化组件，不调用模型，
也不验证远程认证与模型可用性。

| 角色 | 配置位置 | 用途 |
| --- | --- | --- |
| Target | `agent.llm` | 扮演客服，接受演化 |
| Worker | `algorithm.worker.llm` | 改进 Target |
| User Simulator | `benchmark.user_simulator.llm` | 原生客户模拟器；所有领域必需 |
| Judge | `benchmark.judge.llm` | 原生 NL assertion 评分；所选题池包含此类判据时必需 |

各角色不隐式继承配置，可使用 README 中相应的 `FLASHRSI_*` 环境变量。
Judge 示例保留原生默认模型名 `gpt-4.1-2025-04-14`；用户可以显式更换，但需要在
实验记录中说明评分模型。模型名、连接和请求参数交给上游 LiteLLM，原生 Judge
提示与评分规则保持原样。选择的题池需要 Judge 却没有配置模型时，加载即报错。

`agent.max_steps` 控制一次原生对话的最大步数；`agent.max_turns` 是每次客服回复
内部的 harness 轮数，两者含义不同。模型请求超时放在各自角色的配置中。
整题硬超时 `benchmark.task_timeout` 仍仅支持 POSIX；Windows 示例不启用它。
Windows 需要 UTF-8 模式，统一入口已自动设置；直接调用已安装的 `flashrsi` 时，
先设置 `$env:PYTHONUTF8='1'`，否则启动检查会明确报错。

## 本地数据与资源

默认按固定 commit 下载上游 `data/tau2`。已有本地完整 checkout 时，可以配置：

```yaml
benchmark:
  name: tau2
  domain: telecom
  data_path: ./tau2-bench/data/tau2/domains/telecom
```

`data_path` 指向一个领域目录，路径相对 YAML 文件解析；不接受多个领域或划分目录
映射。目录应保留原生 `tasks.json`、`split_tasks.json`、数据库与 policy 文件。
telecom 使用 `db.toml` / `user_db.toml` 及两份手册；airline / retail 使用 `db.json`
和 `policy.md`。任务文件加载后发生变化，执行会拒绝使用旧 Task。

`TAU2_DATA_DIR` 必须指向 checkout 的 **`data/`**，例如：

```powershell
$env:TAU2_DATA_DIR=(Resolve-Path ./tau2-bench/data).Path
```

上游自行追加 `tau2/`。完整资源中还包括 `tau2/user_simulator/*.md`，仅复制领域目录
不足以运行客户模拟器。统一入口保留已有环境变量，否则下载匹配 revision 的资源。

若 Git 网络下载失败，可从上述固定 commit 的官方 ZIP 准备 checkout，先安装本项目
`.[all]`，再安装原生 tau2-bench，设置资源目录后用已安装的
`flashrsi run --config ...`。

## 执行与评分边界

目标模型通过现有 `TOOL: {"name": ..., "arguments": {...}}` 文本协议调用客服工具，
由桥接层交给原生环境。telecom 客户侧工具仍由原生用户模拟器控制。任务隐藏的
用户指令、初始化动作和评分判据不会拼进 Target 提示；这不是对拥有本机文件权限的
Agent 的文件系统安全隔离，数据目录应视作评测基础设施。

指标 `reward` 来自原生 `RewardInfo.reward`，同时在轨迹保留评分明细、终止原因和
脱敏的角色配置。失败轨迹计零分，不使用模型输出中的自报分数。当前每题执行一次，
报告均值；没有实现多次试验的 pass^k 汇总。上游路径和 Judge 默认值是模块全局状态，
当前桥接按整题串行使用并在结束后恢复，避免并发任务互相污染。

当前仅接入三个标准文本领域；未接入语音、多语言、solo、telecom workflow 变体、
多次试验统计或新的 Agent harness。`mode: voice` 等不支持的配置会明确报错。

原生离线用例运行真实环境、工具和评分器，仅替换模型回复；不能算作真实模型演化
smoke。真实算法 × Target × τ² 组合仍需单独验证。
