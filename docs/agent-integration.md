# 接入一个 Agent

优先使用 harness 已有的函数、CLI 或 SDK，保留原生代码。框架接入只需要组件声明和
一个 `Agent` 子类；原生文件不必依赖 FlashRSI。

## 只做两件事

**1. 在接入类旁放 `components.yaml`，声明可演化文件。**

按 harness 原有目录填写路径，例如：

```yaml
code:
  - runtime/**
prompt:
  - prompts/**
skill:
  - skills/**
```

接入代码留在规则之外。基类负责收集文件、导出所选版本和清理版本目录。

**2. 继承 `Agent`，声明 `name`、`description` 并实现 `_run`。**

`_run` 调用原生入口，再把结果转为 `Trajectory`。它用到三个输入：

| 输入 | 用途 |
| --- | --- |
| `self.config` | 当前调用固定的角色配置；模型名、地址和请求参数从 `self.config.llm` 读取，执行参数读取该角色自身字段 |
| `self.files_dir` | 从这里执行所选版本的代码、加载配置与资源 |
| `working_dir` | 传给 harness 的任务或编辑目录 |

返回 `Trajectory` 时填写答案和状态；原生提供 messages、usage 就一并带回，没有则用默认空值。
调用结束前关闭自己启动的进程或 SDK 会话；运行产物放在版本目录外，保存的记录按项目要求脱敏。

目标 Agent 使用 `TargetAgent(config.agent)`，Worker 使用
`WorkerAgent(config.algorithm.worker)`；构造函数拒绝整份实验配置。两种角色各自拥有
独立的 `llm`，适配器不需要判断自己的角色。`timeout`、`max_turns`、权限和
`source_dir` 等执行参数留在角色层；原生资源配置继续从 `self.files_dir` 加载。

初始化会复制配置，之后通过实例的 `config` 调整下一次执行的默认值。单次 `run` 和
整批 `rollout` 都固定开始时的配置；`_run` 只读取本次副本。基类在 messages 前加入
脱敏的 `RunConfig`，记录角色配置和 Snapshot ID；适配器直接返回原生 messages 即可。
该记录包含当前角色配置，不展开后端默认值，也不等同于完整模型请求。
用户参数统一来自 config；模型、凭据和端点直接填写在各角色的 `llm` 中。
原生 CLI 必须使用 env 时，由适配器从 config 构造子进程 env。
登录与连接由框架的角色配置提供：目标和 Worker 分别设置自己的 `llm`，凭据不进入
Snapshot。接入 harness 时重点保留它的工具、skills、hooks、子 agent 等执行能力；
不要求复刻原生产品的账号登录界面或账号管理命令。
日期、二进制、集合等非 JSON 的 YAML 值以 `type: yaml` 和 YAML 文本 `value`
记录，保证 trajectory 可序列化；实验配置文件保留这些值的原类型。
`update(snapshot)` 只选择资源版本，不更改模型或连接配置。

先看短例子：[进程接入](../examples/agents/open_harness/__init__.py)、
[SDK 接入](../examples/agents/closed_harness/__init__.py)。
MiniAgent 的完整实现是 [适配类](../flashrsi/agents/miniagent/__init__.py) 和
[_entry.py](../flashrsi/agents/miniagent/_entry.py)。后者只是它的子进程入口，其他 harness 不必照搬。
Claude Code 的 [适配类](../flashrsi/agents/claude_code/__init__.py) 直接创建官方 SDK 会话，
默认使用原生 `claude_code` system prompt，不预装 skill 或附加指令；
从 `files_dir` 加载按需添加的原生插件（skills、agents、hooks 等）、MCP 和 settings，
将 `working_dir` 传为 cwd，并关闭隐式项目配置。SDK 事件含子 agent 的文本和父子 ID；
适配器负责等有限子任务结束及收集其主 agent 回复，公共 Trajectory 保持不变。
`CLAUDE.md` 可以不存在或为空；版本中提供非空内容时，经 SDK `system_prompt.append`
追加。这是显式选择 Snapshot 资源，不是 CLI 的目录发现与分层加载。默认版本不含这个文件。
会话与子任务输出使用独立临时运行目录，关闭历史 auto memory，仅在 `llm.auth: login` 时从原位置读取原生凭据。
扩展加载失败、SDK 异常和暂停／中止结果返回错误轨迹，并保留脱敏诊断。

Pi 的 [适配类](../flashrsi/agents/pi_agent/__init__.py) 从选定 Snapshot 的
`runtime/pi_native/` 加载纯 Python runtime，按上游 Pi v1.4.0（commit
`4a080252f06caf63a525d7873c5d94d3c23887e0`）抽取并执行非交互 JSON 路径的语义子集。
它直接用 `httpx` 调用 OpenAI Chat Completions、OpenAI Responses 和 Anthropic Messages，
完成多轮工具调用，并将 `session`、agent/turn、user/assistant message、tool execution、usage
等事件保留在 Trajectory 中；运行过程不需要 Node.js、npm、Pi CLI 或任何 npm 包。
Snapshot 中的 `AGENTS.md`、`CLAUDE.md`、`SYSTEM.md`、`APPEND_SYSTEM.md`、`prompts/`、`.pi/agent/skills/`、
`.pi/agent/prompts/`（兼容 `.pi/agent/prompt-templates/`）和 `.pi/agent/settings.json`（工具、compaction、branch summary 与重试设置）由 Python runtime 显式读取，
任务工作目录仍作为工具 cwd。模型使用 `provider/model`；`anthropic/...` 自动使用
Anthropic Messages，OpenAI 兼容端点默认使用 Chat Completions，需使用 Responses 时在
`llm.model_api: openai-responses` 显式选择。凭据只从角色配置读取，不写入 Snapshot。
Pi 声明容器运行依赖 `httpx`、Pillow、`regexrs-py` 和 NumPy（公共容器层自动加入 PyYAML），复用通用容器执行入口，
由环境 integration 传入任务工作目录；不需要针对 Harbor 或各个 benchmark 添加 Pi 分支。
[容器入口回归测试](../tests/test_pi_container.py) 使用打包后的代码、选定 Snapshot、
隔离子进程和本地 HTTP 验证实际文件编辑及轨迹返回；该测试不启动 Docker 或 benchmark verifier。
这个 native port 对齐 Pi 的 headless Target/Worker 路径，支持 Pi v3 JSONL session、自动/手动
compaction 和 branch summary。角色配置用 `session_file` 显式选择会话文件；未配置时使用内存
会话，不自动读取宿主 Pi 历史。持久状态放在 Snapshot 外，每个独立任务使用独立文件。
`compaction.enabled` 默认开启，`reserveTokens`/`keepRecentTokens` 默认 16384/20000；
按固定模型目录解析窗口，也可显式设置 `context_window` 和 `model_max_tokens`。
阈值触发、context overflow 和可恢复的 length 会走 Python 摘要与恢复路径；
任务 prompt `/compact [instructions]` 可以手动压缩。摘要错误或取消不会提交部分 compaction。
`/tree ENTRY_ID` 选择历史分支，并在需要时生成被离开分支的摘要；`branch_summary` 控制该行为，
也可用 `session_leaf_id` 显式恢复已有分支。摘要使用该角色的模型连接与显式重试设置。
交互式 TUI/RPC、TypeScript 扩展运行时和完整 MCP OAuth 生命周期仍不在此实现范围。

轨迹对照用同一组固定模型回复，实际执行上游 `runAgentLoop` 和原始 read/write/edit 工具，
再将原始结果冻结为 [10 个重放 case](../tests/fixtures/pi_trajectories_upstream.json)，由
[Python 回归测试](../tests/test_pi_trajectory_parity.py) 检查消息、控制事件、工具详情、文件字节和结束状态。
该验证固定初始 system prompt，排除流式分块、时间戳、耗时及 provider 额外元数据；
不代表真实模型生成轨迹或全部原始 JSON 一致。另有实际原版生成的 prompt sections、资源发现、
工具 schema/参数校验、OpenAI/Anthropic 请求及重试对照；Python 集成测试覆盖取消和超时的部分轨迹保留。
小图附件保留原始字节；Photon 的 Lanczos 缩放、PNG/JPEG 编码和 VP8 WebP 解码使用纯 Python/NumPy
实现，并对固定原版样本逐字节对照。Bash `onUpdate`、三种 provider 的 SSE 增量事件、程序调用的
`structuredContent`、steering/follow-up 队列和 hooks 均已接入；TypeScript 扩展运行时、完整 MCP
OAuth 和交互式 RPC 仍按上文边界处理。搜索使用 `regexrs-py` 和 Python 文件遍历，已覆盖原版的
regex/glob、排序、分页和 fd 过滤路径；参考执行使用的 Node/typebox/diff 仅用于生成测试结果。
Pi 执行路径只需 Python、httpx、PyYAML、Pillow、`regexrs-py`、NumPy、模型 API 和 shell 命令所需系统程序。
Snapshot 的 `pi_native/reference/` 保存原版默认 prompt 所引用的文档和示例（包括 TS/JS/WASM 等参考数据），
运行时只将这些文件作为可读资料，不加载 npm 依赖或执行其中的 Node 程序。
[压缩测试](../tests/test_pi_compaction.py)和[分支摘要测试](../tests/test_pi_branch_summary.py)
另行执行原版 TypeScript helper 的固定输入对照，并验证 Python 主循环、JSONL 会话、
摘要失败与取消的提交边界。固定回复对照不能证明真实模型准确率；此前真实请求返回 401，
没有得到可用于比较的模型任务得分。
项目基础安装仍包含其他 harness 的依赖，尚未拆为 Pi 的最小安装集。

Hermes 的 [适配类](../flashrsi/agents/hermes/__init__.py) 从选定 Snapshot 的
`runtime/hermes_native/` 加载纯 Python runtime，基于 NousResearch Hermes Agent
v0.22.5（commit `bf0203a794a07aac3ff764972ab9087baa4e4f4e`）抽取 headless 执行逻辑。
上游的迭代预算、思考标签清理、同轮工具去重、文本续写、提示词与 skills helper、
文件读取、fuzzy patch 和上下文压缩代码保留在 Snapshot 中，并携带 MIT 许可和源码哈希。
适配器通过 `httpx` 调用 Chat Completions、Responses 或 Anthropic Messages，模型名按字面传给端点，
不要求 `provider/model` 前缀。`llm.model_api` 默认 `chat_completions`，
Responses 使用 `openai-responses`。每个角色显式配置凭据和端点；不需要安装 Hermes CLI、
上游源码、Node.js 或 npm，不发现宿主 Hermes profile 或 OAuth 登录状态。

默认 Snapshot 提供身份指令 `SOUL.md`、`config.yaml`、一个精简的 `hermes-agent` 说明 skill
以及实际执行代码；用户新增的 skills 也可以放入 Snapshot 的 `skills/`。`SOUL.md` 是指令
编辑算法使用的主文件。角色配置覆盖 Snapshot 同名设置，模型、凭据和
端点始终来自该角色的 `llm`。任务工作目录独立传入，项目 context 按上游规则发现。
默认工具为 `terminal`、`read_file`、`write_file`、`patch`、`search_files`、`todo_list`、
`skills_list`、`skill_view`；压缩启用且未显式限制工具时自动提供 `session_search`，用于查回
压缩前的本地原始消息。用 `enabled_tools` 选择具体工具，或用 `enabled_toolsets` 选择
`terminal`、`file`、`todo`、`skills` 分组；`disabled_toolsets` 可禁用分组。

`patch` 支持 `mode: replace` 定向替换及上游 V4A 多文件更新、添加、移动、删除；
按上游模型分组规则向 OpenAI/GPT 系列声明 V4A 参数。覆盖已有文件前，`write_file` 要求
已读取完整且未变化的当前内容；分页未读完或展示被截断时拒绝覆盖。它也拒绝写入内部行号，
默认校验 JSON/TOML；YAML 语法校验沿用上游的可选 `ruamel.yaml`，最小容器未安装时跳过。
`search_files` 使用 `regexrs-py` 与 Python 文件遍历；结果顺序、分页和正则错误文字仍可能与原版 ripgrep 不同。
运行依赖为 Python、httpx、Pillow、`regexrs-py`、PyYAML、Bash 和任务命令需要的程序。容器复用公共 Snapshot
执行入口，不需要 benchmark 专用 Hermes 分支。示例见 [hermes.yaml](../examples/hermes.yaml)。

OpenClaw 的 [适配类](../flashrsi/agents/openclaw/__init__.py) 从所选 Snapshot 的
`runtime/openclaw_native/` 加载纯 Python embedded-turn runtime，基线为 OpenClaw
`v2026.10.9`（commit `8b18b5578f66eab1501cba4cef2310d88bfb9a41`）。它按固定顺序组装
bootstrap 文件和 skills、恢复 SQLite transcript、执行上下文压缩、请求模型、顺序执行工具，
直到最终答复或迭代预算结束。Chat Completions、Responses 和 Anthropic Messages 各自使用
原生的 assistant tool envelope 和 tool result 格式，并支持 SSE、usage、取消和超时。

Snapshot 提供 `read`、`write`、`edit`、`apply_patch`、`exec`、`process`、`read_skill` 和
`memory_search` 工具；工具策略支持 profile、显式 allow/deny 和 opt-in workspace sandbox。
skill 清单遵循 workspace > `.agents/skills` > bundled 的优先级，并应用
`metadata.openclaw` 的 disabled、OS、binary、environment 和 model-invocation gating。
`PROVENANCE.json` 和 [native runtime README](../flashrsi/agents/openclaw/runtime/openclaw_native/README.md)
记录了来源、范围和明确排除的 Gateway、channel、UI、cron、plugin marketplace 与容器 supervisor。
OpenClaw 路径不启动 Node.js、npm、Gateway 或宿主 CLI；运行依赖只有 Python 项目已有的 httpx 和 PyYAML。
示例见 [openclaw.yaml](../examples/openclaw.yaml)，wire、session、tool loop 和资源测试见
[OpenClaw tests](../tests/test_openclaw_provider_wire.py)。

OpenCode 的 [适配类](../flashrsi/agents/opencode/__init__.py) 从所选 Snapshot 的
`runtime/opencode_native/` 加载纯 Python embedded-turn runtime，基线为
`anomalyco/opencode` v1.19.3（commit `e3c4eb242585dcd77be1a867391b7c25fd47ce52`）。主循环保留
OpenCode 的 prompt/processor 结构：恢复 session、组装 instructions 和 skills、请求 provider、
按顺序处理 tool calls、压缩上下文并在最终 assistant turn 结束；Chat Completions、Responses
和 Anthropic Messages 的 tool envelope 分别按原生协议生成。

这是 headless Snapshot 适配层，不宣称产品级逐事件、逐数据库 schema 或逐工具输出字节一致：SQLite
transcript、事件记录和本地 compaction 是 Python 等价实现；交互式 permission ask、Question、MCP/LSP
监督、模型摘要 compaction、媒体附件与 TUI/server 仍属于明确排除或需要宿主服务的范围。

默认工具包括 `read`、`write`、`edit`、`apply_patch`、`bash`、`glob`、`grep`、`webfetch`、
`diagnostics`、`task`、`todowrite` 和 `skill`，并支持 Snapshot 兼容的 `read_skill`、`memory_search` 与 process
工具。权限配置支持 profile、allow/deny 和 workspace sandbox；session 使用 Snapshot 外的
SQLite transcript，避免不同 benchmark task 共享历史。运行不需要 Bun、Node、npm、OpenCode
CLI 或宿主 server；明确排除 TUI、server、OAuth/account、MCP supervisor 和 LSP process
supervisor。`PROVENANCE.json` 和 [native runtime README](../flashrsi/agents/opencode/runtime/opencode_native/README.md)
记录了来源、适配范围和排除边界。示例见 [opencode.yaml](../examples/opencode.yaml)，核心、wire
和 adapter 测试见 [OpenCode tests](../tests/test_opencode_native_core.py)。

上下文和长输出路径均由所选 Snapshot 内的 Python 代码执行：

| 路径 | 配置与行为 |
| --- | --- |
| full context compaction | `compression.enabled: true` 默认启用，`threshold` 默认 0.50；可用 `threshold_tokens` 设绝对触发上限，用 `context_length` 覆盖固定模型目录中的窗口 |
| 主动历史工具 prune | `compression.proactive_prune_tokens` 大于 0 时启用，保留工具配对及必要的尾部上下文 |
| micro / idle compaction | `compression.micro_compact: true`、`idle_compact_after_seconds` 分别启用；默认关闭 |
| manual / overflow recovery | 任务 prompt 使用 `/compress [focus]` 手动摘要；context overflow 自动压缩重试，可用窗口不足时调整请求输出 cap；手动摘要在自动压缩关闭时仍可用 |
| 压缩边界恢复 | 保留当前用户任务、todo/skill 状态，重置读取缓存和 usage anchor；鉴权失败、空或被截断摘要保留原历史，取消后的晚到摘要不能发布状态 |
| session / checkpoint | 显式 `session_file` 保存消息、todo、压缩状态与 archive；锁定会话并原子写入；`compression.checkpoint_required: true` 要求改写前完成本地持久快照，未提供时保留历史 |
| Responses native compaction | `compression.codex_responses_native: true` 显式启用，由原版模型/端点/能力规则决定是否发送 `context_management`；保留并回放 provider checkpoint，被拒绝后回退本地压缩 |
| 工具输出和资源截断 | 支持 `tool_output` 输出限制、`file_read_max_chars`、长输出落盘/回读及 context 文件动态字符预算；这些操作与历史摘要分别执行 |
| `finish_reason=length` / 迭代预算 | 文本续写会按原版提高输出 cap；截断工具调用先重试；预算耗尽时生成最终答复 |

`session_file` 由角色显式指定，放在 Snapshot 之外；每个独立 benchmark 任务使用独立文件，
避免把另一任务的历史带入执行。`checkpoint_required` 同时关闭无法先执行本地 checkpoint 的
micro 和 server-side native compaction。本地 JSON session/快照不是上游 MemoryManager
checkpoint API、SQLite 事务、数据库 row lease 或 session rotation 的移植。

Responses native compaction 默认关闭。原版 gates 只对支持的模型和路由开放，例如官方
OpenAI 端点上的受支持模型，或显式配置官方 Codex token/端点的对应模型；仅选择 Responses
不代表 native compaction 已启用。可以用 `compression.codex_responses_compact_threshold`
设置 native 阈值；原版规则仍约束请求，且明确的 capability 禁用与 checkpoint 要求优先。
凭据始终由角色配置提供，运行时不会启动登录流程。

IPYNB、DOCX、XLSX、SQLite 读取通过原版标准库抽取器处理；图片/binary 读取按原版拒绝。
可选 anydoc/PDF/OCR、vision/video、web/browser、execute_code、完整 CLI/TUI/gateway、账号管理、
插件/MCP、委派子 agent、后台/PTY/远程 terminal 以及上游完整持久 memory 生态仍未移植。技能资源只读，不自动安装
其依赖或执行模板 shell。coding brief、workspace/operator 指令和 skills focus 有原版 prompt
builder 对照；profile、时间、provider 和 home/scratch 文字另有实际原版函数对照，宿主事实显式传入。
Scratch 使用真实独立目录，`TMPDIR/TMP/TEMP` 指向该路径；持久会话默认保存在其 artifacts 目录。
上游 24h retention、孤儿进程和 worktree 清理服务尚未移植，因此 prompt 不承诺该清理政策。
未宣称整个 CLI prompt 或宿主生命周期逐字/逐事件一致。

Chat、Responses 和 Anthropic Messages 请求及响应现在复用 Snapshot 内抽取的原版 transport，支持已移植的
custom、OpenRouter、Kimi、DeepSeek、Gemini OpenAI-compatible 和 xAI profile hooks，以及未注册 provider
的标准兼容路径。`reasoning_config`、reasoning 拒绝后的 drop/floor、request overrides、cache scope、
tool alias、图片/多模态降级、grammar/schema 修复、credit 恢复、Anthropic signed thinking 和重放签名均有
实际原版对照。Bedrock、Gemini REST、实时 provider catalog 和宿主插件发现仍不在这个 headless port 中；
模型能力与 pricing 使用固定的 models.dev/OpenRouter snapshot，显式配置可覆盖。
FlashRSI 角色中的 `api_max_retries`、`auto_recovery_cycles`、`stall_guards`、
`intent_ack_continuation`、`empty_response_guard` 使用顶层键，映射到原版 `agent` 配置段。

[Chat transport 对照](../tests/test_hermes_chat_transport.py)、
[policy 对照](../tests/test_hermes_policy_parity.py)、[工具测试](../tests/test_hermes_tools.py)、
[资源测试](../tests/test_hermes_resources.py)和[集成测试](../tests/test_hermes_agent.py)分别验证
helper、工具、资源和多轮执行；[容器入口测试](../tests/test_hermes_container.py)使用隔离本地
进程和本地 HTTP，不启动 Docker verifier。[基础轨迹回放](../tests/test_hermes_trajectory_parity.py)
对照实际原版 `AIAgent.run_conversation` 的文本、工具、预算、续写和失败恢复。
[压缩对照](../tests/test_hermes_compression.py)另含 30 个实际原版压缩器场景、8 个原版启用压缩的
完整循环场景，以及 native 的鉴权失败、取消、会话恢复和持久 checkpoint 测试。
[Responses 对照](../tests/test_hermes_native_responses.py)覆盖 116 个实际原版策略、checkpoint、
请求与响应转换及辅助摘要场景；[incomplete 续写对照](../tests/test_hermes_native_continuation.py)
包含 18 个原版场景、44 步状态变化，验证 checkpoint 合并、去重、重试与输出预算。

本轮进一步审计的已修复问题、逐层证据和仍未覆盖的产品边界见仓库本地
`runs/full-parity-audit-20261101/REPORT.md`。Pi 和 Hermes 的 FlashRSI Target/Worker
headless 执行路径、agent loop、provider transport、工具、流式控制、压缩、恢复和 Snapshot
已经接入并通过固定原版回放；Pi/Hermes 的完整上游 CLI、TUI、扩展与账号生命周期仍属于产品边界之外。

轨迹验证使用固定的模型/摘要回复，比较消息、工具动作、结束状态及请求 cap 等控制逻辑。
流式分块、时间戳、provider 元数据和模型生成随机性不在逐字对照范围。它不能证明真实模型的
任务准确率或 benchmark 分数一致；此前真实模型请求返回 401，没有得到可用的准确率结果。

Codex 的 [适配类](../flashrsi/agents/codex/__init__.py) 默认在独立 Python 进程中执行
选定 Snapshot 的 `python_core/`，不调用原生 CLI，也不回退宿主核心。
将 Snapshot 的核心与资源复制到每次调用独立的 home，加载 `AGENTS.md`、skills、
配置、hooks 和显式插件；任务 cwd 仍由 `working_dir` 指定。宿主的技能、插件
和历史记忆不会自动混入所选资源；项目 AGENTS/skills 作为任务输入发现。
未迁移能力有明确边界，例如 rules 执行策略尚未实现。POSIX shell snapshot 默认开启，
生成、条件恢复与清理均由 Snapshot 内的核心负责。
插件只版本化 `plugins/cache/**` 下的已安装 bundle 及其元数据；`plugins/data/**`
等运行状态不从来源目录捕获，插件启用配置保留在 `config.toml`。
模型与凭据通过角色配置映射，Python 核心的 JSONL 事件和脱敏诊断转为 Trajectory，
退出时清理进程与运行状态。服务、hooks 和子 agent 仍需配置注册。Codex 适配器只有 Python 核心执行路径，
不接受 `cli_path`，也不会回退到宿主或 Rust 原生 CLI。接入与验证边界见
[Codex 说明](codex.md)。

## 可选：容器执行

只有接入带 `Task.env` 的任务时才需要这一步；普通问答、文件编辑和 Worker 接入仍按
上面的两步完成。环境执行器由 runner 连接，Agent 不需要实现 Terminal-Bench 或 Harbor 专属钩子。

内置适配器用 `_container_dependencies` 声明容器所需的最小发行包集合，
从安装包元数据读取版本，避免把完整 `all` extra 的 benchmark 依赖带入每个容器。外部适配器
也可用同一属性声明发行包名元组；两者默认 `None` 表示未支持容器。
`_check_container()` 做执行前检查，覆写时先调用基类检查；Claude Code 在这里检查
`cli_path` 和显式凭据。容器直接接收解析后的完整角色 config，不转发宿主环境配置。
版本化资源仍从 `self.files_dir` 读取，容器任务目录仍通过 `working_dir` 传给 `_run`。

Harbor 安装声明依赖在宿主上的对应版本，将框架源码和 Snapshot 分别传入容器，
按可导入的 Agent 类路径创建实例，再调用现有 `run`。外部适配类必须可导入，其发行包
也需列入依赖；任意外部源码不会自动复制。声明容器支持前应确认 harness 能在 Linux 中
运行，并具备目标任务所需的工具。内置 MiniAgent 已通过原生 workspace tool 提供 shell；
容器支持声明本身不检查某个 harness 是否能完成具体任务。
框架入口隔离自身的 Python 导入路径，保留任务环境的 `PYTHONPATH` 供任务工具使用。

CLI runner 为 Terminal-Bench 自动绑定 Harbor。直接 Python 使用在 `rollout` 前调用：

```python
from flashrsi.integrations.harbor import bind

bind(agent)
```

未绑定环境执行器时，`rollout` 会在开始任何任务前拒绝带 `Task.env` 的整批任务。
数据、依赖和验证范围见 [Terminal-Bench 说明](terminal-bench.md)。

## MiniAgent 的调用路径

```text
MiniAgent._run → 子进程 _entry.py → 原生 run → Trajectory
```

适配类通过 stdin JSON 直接传入 `llm_config=config.llm`、`web_config=config.web`、
`debug_llm_io` 和其他原生运行参数。原生 `run` 接收普通字典并初始化当前子进程的
模型与网页工具设置；无需导入框架 Config，也不读取 `FLASHRSI_*` 或 `OPENAI_*` 回退。
`source_dir` 提供的 harness 需支持同样的函数参数；没有返回 messages 时，
最终轨迹仍包含基类添加的 `RunConfig`。

## harness 的小改动

内置 harness 保留以下独立优化；它们不需要 FlashRSI，也不是新 Agent 的接入模板：

| 文件 | 改动 |
| --- | --- |
| `agent.py`、`editor.py` | 增加可选编辑目录，Context 向工具提供任务工作目录；保留自身指令；原生返回已有 messages，保留失败时已产生的输出和用量；检查工具是否启用 |
| `llm.py` | 移除 provider 专用默认值和未使用的 analyzer/LiteLLM 路由；统一使用 OpenAI SDK，保留 Chat/Responses；推理设置和 `extra_body` 显式传入；SDK 重试交给原生循环管理 |
| `trace.py` | 用显式配置和运行环境中的密钥对原生日志脱敏 |
| `workspace.py` | 默认资源目录改为本包的 `agent.yaml` 所在目录；自动发现目录型 SKILL.md，只展示 description 并提供按需读取工具；平铺 skill 继续内联 |
| `tools/read_skill.py`、`tool_descriptions/read_skill.json` | 存在目录型 skill 时自动提供的原生工具，按需读取发现的 skill 正文，不返回 frontmatter；与 workspace 配合，不依赖框架或算法 |
| `tools/shell.py`、`tool_descriptions/shell.json`、`agent.yaml` | 默认启用原生 shell 工具，支持工作目录、超时和有界输出收集；正常完成时保留后台服务，超时或取消清理当前命令进程组；随 Snapshot 版本化，不依赖框架或 benchmark |

`web_config.py` 改为读取传入的 `web` 字典；`reader.py`、`tools.py`、`tool_result.py` 保持原生逻辑。
`usage` 沿用原生统计范围，包含主循环、辅助调用与强制回答；网页提取用量没有额外补齐。

原生差异集中在 [miniagent-harness.patch](miniagent-harness.patch)，对比来源是旧 FlashRSI
提交 `5efe9b438483710060183c6c3ecbc5062b410a58` 的 `miniagent/harness/*.py`。
对比前对基线使用同样的 Ruff 格式化，以排除纯格式差异。

目录型 skill 的发现与加载属于 MiniAgent 原生 workspace 能力：扫描
`skills/<name>/SKILL.md`，展示 description，按需读取正文。目录存在时提供 `read_skill`，
删除最后一个目录后工具随之移除（原生配置显式启用的工具仍保留）。配置中的
`skills` 列表仍只负责原有平铺 `.md` 的全文内联；同名目录和平铺文件会明确报错。
Evo-Harness 只编辑 skill 文件，配置由既有 Snapshot 权限保持只读，无需额外激活声明。
自定义来源目录需要自行保留相应的原生发现和加载能力；算法不会改写其 runtime。

Review 顺序：`components.yaml` → 适配类 `_run` → `_entry.py` → 上述差异文件。
进程桥接在收到终止信号时取消原生执行；适配类给工具最多两秒完成清理，再强制终止。
