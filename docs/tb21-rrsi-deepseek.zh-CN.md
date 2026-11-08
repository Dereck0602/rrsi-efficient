# TB2.1 + RRSI + 本地 DeepSeek

配置：[terminal_bench21_rrsi_deepseek.yaml](../examples/terminal_bench21_rrsi_deepseek.yaml)。
入口：[run_tb21_rrsi.sh](../scripts/run_tb21_rrsi.sh)。入口默认只打印计划，不启动模型推理。

## 已确认的设置

| 项目 | 配置 |
| --- | --- |
| 数据 | 本地 TB2.1，固定提交 `7131e4375048a0e408a8fb404b5f499d726b695b` |
| 划分 | `train: 30, dev: 0, test: 59`，`benchmark.seed: 42` |
| Target 任务并发 | **8**，应用于基线、候选测量、smoke 和最终 test；小集合并发不会超过任务数 |
| Worker 轨迹分析并发 | **8**，`algorithm.digest_concurrency`，只并发同一批独立 digest；analyst 的决策与后续追问仍按依赖顺序执行 |
| Agent 总执行时限 | **max(任务原生时限, 3600 秒)** |
| 评分 | Harbor 原生 verifier；资源与 verifier 时限沿用任务配置 |

名单由实际 TerminalBench 加载器按完整 `task_id` 的 SHA256 排序生成。
当前固定数据中的完整 ID 带 `terminal-bench/` 前缀；不要只对目录名做哈希。
不读取旧 TB2.0 DPP 的 split.json，也不按难度重新抽样。
`train` 同时用于诊断和候选接受判断；`dev` 不使用；`test` 只在选定最终版本后执行。

87 题的有效 Agent 时限为 3600 秒；`sam-cell-seg` 保留 7200 秒，
`build-pov-ray` 保留 12000 秒。后两题均在本次 test 集。
这是 Agent 阶段的总时限，不是整个 trial 的墙钟上限：
环境构建、Agent 安装（600 秒）、清理宽限（30 秒）和 verifier 另有时限。
`agent.timeout` 是固定覆盖值，不能与 `agent.harbor.agent_min_timeout` 同时使用。

## 其他初始参数

这些参数已写入配置，可在正式运行前调整。

| 项目 | 初始值 |
| --- | --- |
| Target / Worker harness | MiniAgent / 独立 MiniAgent 文件编辑 Worker |
| 模型 | 两者均为 `deepseek-v4.1-flash` |
| 搜索轮数 / 每轮候选 | `T: 20` / `m: 2`（RRSI 实现默认值） |
| 每次全量 train 测量 | `k: 2`，60 次执行进入同一队列，共享最多 8 个任务槽位 |
| 任务排序 | `task_order: longest_first`，根据上一轮每题平均完整耗时优先派发慢任务 |
| 噪声带 | `delta: null`，从重复基线自动校准；bootstrap seed 为 7 |
| Target / Worker 轮数上限 | 200 / 30 |
| Worker 每次调用总时限 | 3600 秒 |
| 每次响应输出上限 | 16384 tokens，包含模型推理输出预算 |
| 采样与推理 | temperature 1.0，high，enable_thinking=true |
| 请求尝试次数 | Target 1 次；Worker 最多 3 次（包括首次），重试间隔 2 秒、4 秒 |
| 最终 test | 每题 1 次；`k: 2` 不作用于最终 test |

MiniAgent 的单次 HTTP 请求沿用当前 OpenAI SDK 超时（连接 5 秒、读写 600 秒）；
shell 工具默认单命令 60 秒，模型可显式设置命令 timeout。
这些限制与 Agent 总时限独立，任务也可能先达到轮数上限。

Worker 使用 `llm.llm_retry_policy: transient`，仅对 HTTP 502/503/504、
连接异常和连接/读取等请求超时重试；鉴权、参数、输出格式、输出 token 预算等错误不在这一 HTTP 重试层重试。
重试发生在单次模型请求内，保留已有对话与工具执行结果，不重新执行整个 Worker。
辅助调用和最终回答也使用该策略；SDK 自带重试关闭，避免叠加次数。
等待和重试都计入 Worker 原有的 3600 秒总时限，取消会终止退避等待。
失败尝试记录在 `MiniAgentModelRequestError` 中；即使随后成功，未返回用量的
尝试仍使 `accounting_complete=false`，已知 token 用量照常保留。
用尽次数仍失败时，沿用 Worker 报错退出逻辑。
新设置适用于采用当前 MiniAgent 源码的新运行；运行中的进程和已保存的旧快照不会热更新。

RRSI 另外设置 `algorithm.worker_output_retries: 1`：Worker 明确返回输出截断
（`length` / `max_output_tokens`）时，在相同 16384 token 上限和推理设置下，
将受影响的候选提案阶段或整轮分析阶段重新执行一次。候选从 incumbent 的新目录
重新开始，分析等待并发 digest 清理后重做；第二次仍截断则拒绝候选或跳过该轮，
保留当前最佳版本，继续后续候选和轮次。各次 Worker 调用仍分别受原有 3600 秒时限约束，
所以重试会增加整个阶段的总耗时及用量。设为 0 可直接跳过。
失败用量与轨迹继续落盘，`worker-output-budget` 记录 retry/skip 动作。
该处理位于 RRSI 算法层，使用已保存 Worker 快照的新进程也能生效；不修改模型预算或快照。

在每个候选都通过前置检查的情况下，Target 任务执行上限为：

```text
基线：30 × 2                     =   60
候选：20 × 2 × 30 × 2           = 2400
smoke：20 × 2 × 2               =   80
最终 test：59                    =   59
合计                            = 2599
```

以上不包括 Worker 的分析、digest、编辑、critic 与修复调用。
并发 8 不表示 8 个候选同时搜索，也不表示固定 8 个 GPU。
脚本保持 RRSI 候选生成、测量与接受的原有顺序，只并行同一版本的任务。
最终返回最后接受的版本；Runner 不自动补跑初始版本在 test 上的对照。

超时任务会尽量恢复已刷入 trace 的 token 用量，并标记统计不完整。
仅缺少用量不会终止 RRSI。此本地实验启用 `algorithm.cost_policy: estimate`：
原始 `cost`、`tokens`、`usage_complete` 和 API 用量保留不变，估算单独记录在
`cost_accounting.estimate`。完整用量仍按原始分数/成本规则选择。

部分缺失且有完整可读的串行请求记录时，以已知用量加最近最多 5 次请求的中位数
估算尾部；记录到的失败请求增加相应份数的开销。敏感性范围从已知用量到
已知用量加两倍最近最大请求开销。没有可用请求尾部时，只参考同一测量、同一
harness、同一任务的完整重复执行：取均值，敏感性上端取最大值的两倍；
有已知用量时，估计和范围均不得低于它。找不到参考则继续标记不可比较，
不会拿其他题目或其他 harness 的用量补零。

最多允许 25% 的任务执行需要估算（`cost_estimate_max_frac: 0.25`，独立于错误率门槛）。
综合规则采用“候选敏感性上端 / 当前版本已知下端”计算相对成本，另存点估计供核对。
这些是启发式敏感性范围，不是置信区间或严格成本上界。估算不可用或覆盖比例超限时，
退回“分数提升超过 δ”的规则；历史分数底线、guards 和错误率限制始终保留。
可将 `cost_policy` 设为 `complete` 恢复只用完整用量比较的策略。
**本实验失败率限制为 25%**（`algorithm.invalid_missing_frac: 0.25`）：超过限制时基线中止，候选测量被拒绝；error 任务仍按零分计入分母。每次测量共 30 × 2 = 60 次任务执行，允许最多 15 次 error，第 16 次起超限。

## 使用

在 FlashRSI 根目录执行：

```bash
bash scripts/run_tb21_rrsi.sh plan
bash scripts/run_tb21_rrsi.sh check
# 正式启动完整进化，前台运行：
bash scripts/run_tb21_rrsi.sh run
```

`plan` 只读取配置和本地任务，输出完整名单、checksum、逐题时限和执行预算。
`check` 额外检查 Docker、模型列表和 RRSI 初始化，不执行题目或模型推理。
`run` 先在 `runs/tb21-rrsi-deepseek/plans/` 保存计划，再开始完整实验；
每次运行生成新的实验目录。对于完整基线已经落盘、尚未开始进化的中断运行，
可复用基线继续进化：

```bash
bash scripts/run_tb21_rrsi.sh check --resume-from runs/tb21-rrsi-deepseek/原实验目录
bash scripts/run_tb21_rrsi.sh run --resume-from runs/tb21-rrsi-deepseek/原实验目录 --progress log
```

恢复会检查配置、完整的 `k` 次基线、任务顺序/checksum 和快照身份；恢复原始
Target / Worker 文件快照，跳过基线模型调用，重新计算统计与噪声带后从第 1 轮开始。
允许调整错误率阈值、digest 并发、任务排序和成本估算策略；模型、输出预算、任务时限等执行配置不匹配时拒绝复用。
外层 adapter 的超时用量恢复和最新 RRSI 记账容错仍然生效。
新目录的 `resume.json` 保存来源、文件哈希与复用次数，旧目录保持原样。
汇总包含继承的基线用量并注明来源，不能把这部分当作新增调用开销。
普通 `--resume-from` 只支持完整基线后的恢复，不恢复半批任务。

第一轮所有候选已完成测量时，可显式重算第一轮接受决定，从胜出版本继续第二轮：

```bash
bash scripts/run_tb21_rrsi.sh check --resume-from runs/tb21-rrsi-deepseek/原实验目录 --reselect-round-one
bash scripts/run_tb21_rrsi.sh run --resume-from runs/tb21-rrsi-deepseek/原实验目录 --reselect-round-one --progress log
```

此入口校验原始任务、分数、用量、快照、配置和 δ，复用基线及第一轮候选测量，
不重跑这些任务。新目录保留原始测量并新增 `cost-reestimate`、接受历史和恢复来源；
旧目录不修改。恢复原始 Worker，继承第一轮分析、接受历史和任务耗时，保持 `T=20`
的轮次编号及退火进度，从 `round=2/20` 开始，后续还有 19 轮及最终留出测试。
旧第二轮及后续工作基于旧 incumbent，均不参与新分支；这部分已消耗资源仍保存在
原目录，未计入新分支继承的用量。该入口目前要求 `calibration_evals=1` 且第一轮
每个候选都有完整测量记录，不是通用的任意轮次恢复。

### 进度显示

默认 `run.progress: auto`：前台终端使用动态进度条；`nohup`、重定向或
`tee` 写日志时，改为可直接在 IDE / `tail -f` 中查看的逐行进度。
每个任务结束后立即刷新；较长的等待期间每 30 秒输出一次状态。

进化显示已完成轮数 `rounds 0/20`、当前轮次 `round=1/20`、
候选 `candidate=1/2`、重复次数 `repeats=2`，以及 baseline / smoke /
candidate 阶段。完整测量显示合并后的 `0/60` 进度，`last` 的
`[repeat=1/2]` 或 `[repeat=2/2]` 标识该次执行。最终测试单独显示
`final-test` 的 59 题进度。
Worker 的分析、digest、编辑和 critic 阶段也会打印标记。
每轮结束另有 INFO 成绩摘要，例如 `RRSI round=1/20 complete | evolve
current=56.67% best=56.67% baseline=43.33% gain=+13.33pp | accepted=yes`。
`current` 是下一轮采用版本的进化集得分；`best` 是包括基线在内、历轮已接受版本
的最高分，不包含被拒候选。`gain` 为最高分相对基线提升的百分点。
该摘要不依赖进度条开关，同时保存为 `*-round-summary.json`，包含当前和最优快照 ID。
完整测量开始时打印 `task order=longest_first; historical timings=30/30`。
第一轮使用基线的历史用时，同轮两个 candidate 共用一份预测；下一轮使用上一轮
各候选完整测量的平均用时更新。用时覆盖环境准备、Agent、verifier 和清理，
不包含排队；首次基线没有历史时保持原顺序。分数和 token 仍按原始任务/repeat 顺序记录。
排序依据与实际用时保存在 `*-measurement.json` 的 `scheduling` 和 evidence 中，
不会根据得分筛掉任务或使用 test 数据。可设 `task_order: fifo` 对照比较。
轨迹分析另有 `digests` 进度条，其 `active` / `queued` 表示本批正在调查和排队的轨迹数。
`rounds` 进度条使用 `active_rounds` / `queued_rounds`，表示进化轮数，不能当作模型请求并发。
analyst 每次选择 1–8 条调查请求，实际 digest 并发为该批请求数与配置上限的较小值。

下面是格式示意（并非实测结果）：

```text
PROGRESS ... RRSI | round=1/20 | candidate=1/2 | repeats=2 | candidate | tasks [########------------] 24/60 (40%) active=8 queued=28 errors=0 cancelled=0 elapsed=00:20:00 ETA~00:30:00 state=running last=terminal-bench/fix-git [repeat=1/2]:finished
PROGRESS ... final-test | test | tasks [##########----------] 30/59 (51%) active=8 queued=21 errors=1 cancelled=0 elapsed=01:00:00 ETA~00:58:00 state=running last=terminal-bench/example:finished
```

`tasks` 是已结束的任务数，包括正常返回和执行错误，不等同于答对题数。
Harbor 任务要完成环境准备、Agent 执行、verifier 和清理后才计数；
`active` 因而包含这些阶段，并不表示正在同时发出同等数量的模型请求。
`errors` 统计执行错误，reward=0 本身不算执行错误。取消的任务单独统计，
不会被填入已完成进度。ETA 是按本批已完成任务吞吐粗估，任务时长差异较大时
会波动；它不是整个 RRSI 实验的结束时间。

可显式选择输出形式：

```bash
bash scripts/run_tb21_rrsi.sh run --progress auto  # 自动选择
bash scripts/run_tb21_rrsi.sh run --progress log  # 始终逐行，便于保存日志
bash scripts/run_tb21_rrsi.sh run --progress bar  # 强制动态进度条
bash scripts/run_tb21_rrsi.sh run --progress off  # 关闭进度，其他日志仍保留
```

已有 Python 进程不会自动加载新代码；进度功能从下次启动生效，历史日志不会补写。
直接通过 Python 调用 Agent / Algorithm 时，默认保持静默；需要显示时可用
`flashrsi._progress.progress_session()` 包住异步调用。Runner 的进化和独立测试入口
会自动开启进度。

可显式覆盖参数，划分不变：

```bash
bash scripts/run_tb21_rrsi.sh plan --rounds 3
bash scripts/run_tb21_rrsi.sh run --rounds 3 --concurrency 8 --min-timeout 3600
```

脚本默认使用仓库 `.venv/bin/python`，也可通过 `FLASHRSI_PYTHON` 指定已有的
Python 3.12+ 环境。环境需要仓库依赖和 `harbor==0.23.0`；入口不自动修改依赖。
其他机器可按项目安装说明准备环境，例如 `python -m pip install -e '.[harbor]'`。

可提前准备 verifier 依赖，减少运行时下载失败。下面的命令只准备错误报告涉及的
任务，也可用 `--tasks openssl-selfsigned-cert qemu-startup` 指定任务目录名：

```bash
.venv/bin/python scripts/prepare_tb21_verifiers.py plan --failure-report /path/to/error-analysis.json
.venv/bin/python scripts/prepare_tb21_verifiers.py build --failure-report /path/to/error-analysis.json
# 准备本配置的完整进化集；从实际 train 划分取名单，不涉及留出集：
.venv/bin/python scripts/prepare_tb21_verifiers.py build \
  --evolve-config examples/terminal_bench21_rrsi_deepseek.yaml --shared-cache
# 按同一预装协议准备其余 59 个留出任务，只安装依赖，不运行题目或推理：
.venv/bin/python scripts/prepare_tb21_verifiers.py build \
  --heldout-config examples/terminal_bench21_rrsi_deepseek.yaml --shared-cache
```

脚本串行生成 `flashrsi-tb21-verifier/<task>:<fingerprint>` 本地派生镜像。
原始镜像、任务文件和任务 checksum 保留；每个镜像只安装该任务 `test.sh`
声明的 apt/Python 依赖及其启动所需的构建产物，不复制测试文件或解答。
使用 uvx 的任务按原脚本选择 Python 3.13.9 或 3.11.14；mailman 使用 3.12.14，
安装工具 uv 固定为 0.9.5；原生使用 pip/pytest 的任务保留镜像的系统 Python，
只提前执行原脚本声明的 pip 安装。顶层 Python 包沿用题目版本，完整依赖清单保存在
镜像的 `/opt/flashrsi-verifier/requirements.lock`。下载缓存位于
`runs/tb21-verifier-images/download-cache`，默认在 `/data` 上。
MTEB 1.36.8 的未约束间接依赖会拉取已删除所需 API 的 Transformers 5，准备时额外
固定 `transformers==4.55.0`、`sentence-transformers==4.1.0`；题目声明的顶层版本不变，
兼容约束和实际安装版本记入 manifest。公开模型权重另记录 SHA256，源码依赖记录提交。
`--shared-cache` 将新增 uvx 任务的解释器和 venv 放在该目录下的 `runtimes/`，
运行时只读挂载到 `/opt/flashrsi-verifier`，减少 Docker 分区占用；每次任务的
容器、上下文和输出仍独立。系统包安装在派生镜像中；已存在的依赖镜像保持原样。
缓存的 spec 与 lock 校验值记录在 manifest，缓存目录不能移走或修改。
准备阶段的 apt 索引、下载包、pip/uv 缓存、源码构建与临时目录也放在输出目录所在
文件系统，默认均为 `/data`。派生镜像仅保留系统包、必要的命令行二进制和原生 pip
任务的 Python 包；大型隔离 Python 环境通过只读挂载使用。
该方式仅支持本地单服务 Docker；原生 pip 任务的包仍安装在派生镜像的系统 Python 中。

每个镜像必须通过 `--network none` 下的启动、包版本及 pytest 插件检查，
才会写入 `runs/tb21-verifier-images/manifest.json`。失败可重新执行 build，
已验证的镜像会复用。Docker 分区可用空间默认低于 8 GiB 时停止准备；不清理其他镜像。
使用 `/data` 只读缓存、确认新增镜像大小后，可用 `--min-docker-free-gib` 明确设置
本次准备的空间底线；该参数不改变 RRSI 运行配置。
默认检查普通用户可用空间。若 Docker 以 root 运行且需要计入文件系统预留空间，
可显式加 `--include-reserved-space`，使用 `statvfs.f_bfree` 检查实际空闲容量，
仍保留默认 8 GiB 底线；非 root 或 rootless Docker 拒绝此选项。该选项不修改
文件系统预留比例，也不清理任何镜像。
对 Debian 11，准备阶段使用官方 `archive.debian.org` 下载已归档的安全更新包，
继续校验 apt 签名；这个临时源配置不会替换任务镜像原有的 apt 源配置。

在 `agent.harbor` 中设置 `verifier_dependencies_manifest` 指向该 manifest 后，
运行器按任务选择固定的镜像 ID。验证阶段上传测试目录时，仅将 `test.sh` 的
联网安装步骤和 `uvx` 启动前缀替换为已安装的隔离环境；测试文件、pytest 参数、
退出码到 reward 的判断均保留。脚本 hash 或镜像配置不符会拒绝使用缓存。
未列入 manifest 的任务仍走原生执行路径。计划文件会列出实际覆盖的任务，
每条 HarborVerifier 记录也会保存依赖镜像信息。
本机 DeepSeek 配置已启用该 manifest，目前覆盖 30 个进化任务及 59 个留出任务，
原有 30 条环境记录保持不变。覆盖与断网导入报告保存在
`runs/tb21-verifier-images/heldout-coverage-20261107.json` 和 `heldout-import-audit.json`。
新机器使用此配置前需运行上述 build 命令。
运行器在进程启动时固定 manifest 中的镜像与缓存信息，更新 manifest 不会热更新
正在运行的实验；新增覆盖从下一次启动生效。

该协议在 Agent 开始前预装依赖，Agent 能看到评分器包及版本，并可能直接使用新增
系统工具；它属于定制环境，不应将其成绩直接等同于原生 TB2.1 条件下的成绩。
预处理不复制测试文件、期望输出或参考解答进镜像；测试数据仍由 Harbor 在评分阶段
上传。只读挂载防止共享缓存被改写，不承担隐藏评分器依赖的作用。

预装环境会改变可用依赖，因此应在该环境下重新测量基线及校准 δ，不能将旧环境
中受下载失败污染的基线直接当作新环境的比较基准。准备镜像不调用模型，也不启动 RRSI。

Agent 自身的 Python 环境与 verifier 环境独立。若任务在 Agent setup 阶段下载
uv/Python/运行依赖失败，可在已经准备好的 verifier 镜像上继续预装 MiniAgent：

```bash
.venv/bin/python scripts/prepare_tb21_agent_runtime.py --tasks openssl-selfsigned-cert cancel-async-tasks
```

该命令只更新指定任务的本地 manifest 镜像 ID。缓存位于
`/opt/flashrsi-runtime-cache`，包含 Python 3.12 和宿主机声明的固定版本依赖；
启动时仅在依赖清单一致且已安装版本校验通过时复用。构建结束会在断网条件下
执行真实 Agent setup 和 verifier 依赖检查，原始镜像保留。

本机完整运行使用共享只读 Agent 环境，覆盖所有任务，避免为每道题再构建一层镜像：

```bash
.venv/bin/python scripts/prepare_tb21_agent_runtime.py --tasks openssl-selfsigned-cert --export-shared
```

将输出目录配置为 `agent.harbor.agent_runtime_cache_dir`（本机 YAML 已配置）。
仅适用于本机单容器 Docker 任务；挂载只读，任务文件、上下文和结果仍各自独立。
它只提供 Agent 依赖，verifier 仍按 `verifier_dependencies_manifest` 的任务覆盖范围选择环境。

可用两个原始进化集任务检查完整进化流程：

```bash
.venv/bin/python scripts/run_tb21_rrsi_smoke.py plan
.venv/bin/python scripts/run_tb21_rrsi_smoke.py run
```

此脚本复用本机配置，仅将进化轮数缩为 1 轮，默认选择上述两题，保留 k=2、m=2
和并发上限 8。末尾评测也复用这两题以检查结果保存，不调用原始 59 题留出集。
记录单独保存在 `runs/tb21-rrsi-deepseek-smoke`，并明确标记为 integration smoke；
其分数、δ 和最终评测不能视为正式实验结果。候选被 gate 拒绝时，结果会注明是否
实际执行过候选测量，不能仅凭进程退出码认定所有阶段均已覆盖。

Target 在 Docker 内使用本机 bridge 地址 `http://172.17.0.1:30166/v1`，
Worker 在宿主机使用 `http://127.0.0.1:30166/v1`。
`local-no-auth` 仅是当前无鉴权服务所需的非空占位值。
可用既有 `FLASHRSI_AGENT_MODEL/API_KEY/BASE_URL` 和
`FLASHRSI_WORKER_MODEL/API_KEY/BASE_URL` 环境变量分别覆盖。
更换机器或 Docker 网络后需重新检查容器可达性。

`check` 的 API 检查发生在宿主机；模型工具调用、完整 usage 和真实任务环境安装仍需 live trial 验证。
这里的成绩应标为“TB2.1 59 题内部留出集”，并注明自定义 Agent 时限。
