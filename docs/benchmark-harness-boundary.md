# Harness 演化与 benchmark 任务环境

演化的对象是 harness：agent loop、上下文/观察管理、调用工具的机制、完成判断、prompt、skill 及原生扩展。
Snapshot 固定这些资源。迁移时用同一个版本执行目标 benchmark，不把目标任务的环境配置提交成新的 harness 版本。

Benchmark 及其接入层负责准备本任务所需的数据、工作目录、服务、领域工具、工具说明、输出协议和评分器。
纯文本 benchmark 可以只提供题目与公开上下文。为隔离宿主机上的标签、数据缓存和历史实验，已接入的纯文本任务通过 Docker 执行 Target，保留原生 harness 工具能力。
Judge、User Simulator、Postprocess 的配置和凭据属于 benchmark，不能从演化 Worker 继承。

“工具”需要按职责区分：MiniAgent 的通用 shell 调度、Claude Code/Codex 的原生工具调用机制属于 harness；
AppWorld 应用 API、ALFWorld 动作服务、网页检索后端、工作簿和代码仓库属于任务环境。
领域工具通过当前接入使用的 shell/CLI、文件或对话协议提供，不要求每个 benchmark 都改用 MCP。
任务专用 MCP 若有需要，也应通过接入层和 harness 的原生运行配置连接；目前没有注册项依赖一个统一的 MCP 注入接口。

## 纯文本任务的执行隔离

SearchQA、MuSiQue、TriviaQA、NQ、2WikiMultiHopQA、MATH、AIME、BBEH、SuperGPQA，以及 HotpotQA `context` 模式共用 `supplied_context` 执行器。CLI 的演化和冻结 Snapshot 评估都会自动绑定它。启动检查要求 Docker CLI 和 Linux daemon；Target 还必须声明容器依赖并支持容器执行。

每题使用独立容器，只暂存框架运行代码、选中的 harness Snapshot、Target 配置和公开题目。原始标注数据、其他实验目录、宿主机工作目录及 Docker socket 不挂载到容器。评分仍在宿主机完成，任务 ID、划分、评分协议和选优规则保持原样。容器需要访问模型端点；这一步没有限制任意网络访问，也没有隔离带工具的 Worker。

直接使用 Python API 时，在加载任务并调用 `agent.rollout(...)` 前绑定同一执行器：

```python
from flashrsi.integrations.supplied_context import bind

bind(agent)
trajectories = await agent.rollout(benchmark.test)
graded = benchmark.grade(trajectories)
```

未绑定的 `rollout` 会报错，不会自动在宿主机解题。其他服务型 benchmark 仍使用各自接入层，不能直接替换为这个只接收题目的执行器。

## 当前 30 个注册项的分工

下表按当前支持的范围说明数据流，不表示已执行 30 个真实迁移实验。
“题目/上下文”表示接入不要求额外领域工具；harness 自带的通用能力仍按自己的配置运行。

| Registry ID | benchmark 提供的环境、工具或公开输入 | 评分边界 |
| --- | --- | --- |
| `alfworld` | 原生文字游戏、初始观察、动作客户端和步数限制 | 宿主端读取原生任务结果 |
| `appworld` | 应用数据库、世界服务、AppWorld 客户端和 API 使用说明 | 独立原生评分容器 |
| `bfcl` | 原生多轮会话、函数定义、调用客户端和回合协议 | 独立官方回放评分 |
| `workbench` | 每题数据库副本、原生工具服务和调用客户端 | 宿主端原生评分 |
| `harvey_lab` | 事项文档、输出目录及交付协议 | 独立官方 Judge 与产物收集 |
| `officeqa_pro` | 公共文档语料副本和语料路径 | 宿主端官方答案评分 |
| `travelplanner` | sole-planning 原生题目及其中的参考信息 | 独立格式转换模型及原生约束评分；评分数据库不交给 solver |
| `tau2` | 原生领域数据库、policy、领域工具、用户模拟器与会话协议 | 原生模拟器评分 |
| `tau3_banking` | 银行领域数据、知识检索工具、policy、用户模拟器与会话协议 | 原生模拟器评分 |
| `spreadsheetbench2` | 工作簿、任务容器及其 Python、输入输出路径 | 独立 LibreOffice 重算与原生评分容器 |
| `browsecomp` | 临时搜索/读网页客户端及检索后端 | 宿主端独立 Judge |
| `deepsearchqa` | 临时搜索/读网页客户端及检索后端 | 宿主端独立 autorater |
| `widesearch` | 临时搜索/读网页客户端及检索后端 | 宿主端官方表格评分和独立 Judge |
| `searchqa` | 题目与原生检索片段 | 宿主端配置的 EM/F1 评分 |
| `hotpotqa` | context 模式提供上下文；fullwiki 模式提供检索客户端和语料服务配置 | 宿主端官方答案/证据评分 |
| `twowiki_multihopqa` | 题目与公开上下文 | 宿主端答案评分 |
| `musique` | 题目与公开段落 | 宿主端答案/支持段落评分 |
| `triviaqa` | 题目及配置范围内的公开证据 | 宿主端原生答案别名评分 |
| `nq` | 题目及配置范围内的公开上下文 | 宿主端原生短答案评分 |
| `swe_bench` | 原生任务镜像、初始代码仓库、问题说明及补丁收集 | 独立官方 SWE-bench verifier |
| `swe_bench_pro` | 原生任务镜像、初始代码仓库和任务说明 | 原生独立 verifier |
| `terminal_bench` | Harbor 原生任务的镜像、文件、程序及任务说明 | 原生 verifier；可配置宿主端 integrity review |
| `deepswe` | Harbor 原生任务、代码环境及模型端点网络约束 | 原生 verifier |
| `jobbench` | Harbor 原生任务环境、文件与任务说明 | 独立原生 verifier，Judge 凭据只交给 verifier |
| `bbeh` | 题目与格式要求 | 宿主端原生评分 |
| `supergpqa` | 题目与选项 | 宿主端答案评分 |
| `hle` | 支持范围内的纯文本题目及回答协议 | 宿主端独立官方 Judge |
| `math` | 题目与回答格式要求 | 宿主端数学等价评分 |
| `aime` | 指定年份的题目与回答格式要求 | 宿主端整数答案评分 |
| `livemathbench` | 指定版本/语言的题目、回答协议与采样设置 | 宿主端独立数学 Judge |

各项依赖、配置和当前支持子集见 [Benchmark 配置索引](benchmark-quickstart.md)。
容器由本项接入准备，不使用上一个 benchmark 的运行目录或镜像来代替。
同一 harness 要有该接入需要的执行能力，例如能通过 shell 调用领域客户端；
领域工具准备齐全不代表任意删去原生工具能力的 harness 仍然可运行。

## 接入与迁移约束

1. 先初始化 harness，并在迁移评估中加载选定的 Snapshot，再绑定目标 benchmark。
   接入所做的配置/能力检查应针对这个版本，不能先检查 seed 再换成另一个版本。
2. `bind` 只连接执行方式和任务服务，不能调用 `update` 替换选定版本。
   runner 在绑定后检查 Snapshot ID；若接入替换版本，启动即失败。
3. 领域工具和任务文件放在临时工作目录、服务或任务容器里；说明通过运行时输入传入。
   harness 从 `files_dir` 读取自身版本，通过 `working_dir` 操作任务，不能把二者合并。
4. 评分器保留标准答案、隐藏测试和评分凭据。传给 solver 的是公开任务数据和操作入口。
5. 子进程或容器必须恢复调用方的 harness 和 Snapshot；不能硬编码另一种 Agent，
   也不能把演化后的文件重新捕获为一个没有原版本来源的新 seed。
6. 不按组件名静默丢弃或覆盖文件。历史代码或 prompt 写死旧领域 API 时，原样迁移可能失败；
   这属于待测策略的迁移表现。若修改策略或替换组件，必须作为新的版本单独报告。

这些约束覆盖演化运行和独立迁移评估，算法仍只通过 Agent 与 Benchmark 的公共接口工作。
τ 的原生模拟器负责外部用户/环境交互，每个 Agent 回合仍调用所选 harness 自己的执行入口；
这里没有声称把模拟器的交互协议也变成可演化的 MiniAgent 代码。

## 验证范围

`tests/test_benchmark_harness_contract.py` 对 30 个注册项及 HotpotQA fullwiki 分支检查：
绑定时已选定演化版本、目标绑定路径正确、任务文件和说明走目标运行目录/输入、
Snapshot 和 Target 配置不被环境替换。外部服务由测试探针替代。

`tests/test_benchmark_snapshot_boundary.py` 另走 AppWorld 的实际 staging 路径、
SpreadsheetBench2 的实际任务构建及 MiniAgent 原生子进程，检查同一个演化 loop 和 skill 继续生效；
Docker 和模型服务仍是测试替身。

`tests/test_tau_task_harness.py` 检查 τ 的独立超时进程入口恢复三个内置 Target Agent 的选定版本。
这些均为离线契约验证，不能替代数据下载、真实服务连接、Docker 运行、官方评分或实际迁移得分实验。
