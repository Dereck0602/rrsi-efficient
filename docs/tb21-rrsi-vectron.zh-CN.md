# TB2.1 + RRSI + Vectron DeepSeek

入口：[run_tb21_rrsi_vectron.sh](../scripts/run_tb21_rrsi_vectron.sh)。
配置：[terminal_bench21_rrsi_vectron.yaml](../examples/terminal_bench21_rrsi_vectron.yaml)。

Target 和 Worker 均使用 `DeepSeek/DeepSeek-V4.1-Flash-1017`，
地址为 `https://api.vectron.meta-stone.com/v1`。宿主机和任务容器使用同一地址。
本入口复用已有的 TB2.1 启动器，并使用独立的 `runs/tb21-rrsi-vectron/` 输出目录。

## 参数

沿用[本地实验的数据与评测协议](tb21-rrsi-deepseek.zh-CN.md)：

- 数据划分为 30 / 0 / 59，数据 seed 42；RRSI 为 T=20、k=2、m=2。
- Target 任务并发 8：每次测量的 30 × 2 次执行进入同一队列，两个 repeat
  合计最多 8 个任务运行，每次使用同一 harness 的独立环境和上下文。
  Worker digest 并发 8。
- `task_order: longest_first`，使用上一轮每题平均完整耗时优先派发慢任务；
  同轮候选共享预测，下一轮更新，首次基线没有历史时保持 FIFO。
- Agent 时限为原生时限与 3600 秒的较大值；Worker 每次执行 3600 秒。
- 错误率上限 25%；基线超限则退出，候选超限则拒绝。
- 输出上限 16384，reasoning_effort=high，temperature=1.0。
- Target 请求尝试 1 次；Worker 使用 `llm_max_retries: 3`、`llm_retry_policy: transient`，
  每次请求最多尝试 3 次，间隔 2 秒、4 秒，仅重试 502/503/504 与连接类错误/请求超时。
- 请求体沿用 `chat_template_kwargs.enable_thinking=true`。
- 单次模型请求沿用 SDK 的读取超时 600 秒，与 Agent 总时限独立。

这是新的模型实验，须重新执行 60 次基线任务；不能复用本地 DeepSeek 的基线结果。

Worker 重试保留当前上下文和已执行工具结果；普通、辅助和最终回答请求均覆盖。
重试与等待计入 3600 秒总时限，失败尝试的用量缺失不会当作零消耗，
成功后仍保留 `accounting_complete=false`。用尽次数仍失败时继续抛出错误。
当前运行中的进程和已保存的旧 Worker 快照不会自动获得该策略。

## 凭据

脚本优先读取环境变量 `VECTRON_API_KEY`；未设置时读取项目根目录的
`.env.vectron`。该文件应只包含 `VECTRON_API_KEY=你的密钥`，权限设为 `600`，
已经被仓库的 `.env.*` 规则忽略。可用 `FLASHRSI_VECTRON_ENV` 指定其他私有文件。
脚本将此 key 分别传给 Target 和 Worker，公开 YAML 中不保存 key。
已有 `FLASHRSI_AGENT_MODEL/BASE_URL`、`FLASHRSI_WORKER_MODEL/BASE_URL` 环境变量
不会把这个入口切换回本地服务；模型与地址以本配置为准。

## 使用

在 FlashRSI 根目录执行：

```bash
# 只查看计划，不联网、不推理；不传参数时也执行 plan。
bash scripts/run_tb21_rrsi_vectron.sh plan

# 检查模型列表和本地依赖，不推理、不执行任务。
bash scripts/run_tb21_rrsi_vectron.sh check

# 启动完整实验，前台显示逐行进度。
bash scripts/run_tb21_rrsi_vectron.sh run --progress log
```

需要保存控制台日志时：

```bash
mkdir -p runs/tb21-rrsi-vectron/logs
set -o pipefail
bash scripts/run_tb21_rrsi_vectron.sh run --progress log 2>&1 \
  | tee "runs/tb21-rrsi-vectron/logs/run-$(TZ=Asia/Shanghai date +%Y%m%d-%H%M%S).log"
```

`check` 的模型列表检查只覆盖宿主机连通性和模型名称，不能证明工具调用、
thinking 参数、token 用量统计或全部任务容器的网络可用性。
2026-11-06 另做了一次宿主机 API 短请求验证：同一模型与请求参数、512 token
输出上限，成功返回 `echo` 工具调用、`reasoning_content` 和 token 用量
（输入 312、输出 52，其中 reasoning 16）。该检查没有执行 TB 任务或完整进化。
`--rounds`、`--concurrency`、`--min-timeout`、`--progress` 参数与原入口一致。
同模型、同执行配置且只完成了基线的 Vectron 实验可用 `--resume-from` 复用基线。
