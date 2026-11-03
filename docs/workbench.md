# WorkBench

接入 [olly-styles/WorkBench](https://github.com/olly-styles/WorkBench)，固定 commit
`49c7dfd00c03d384ec59ea57374f50b766aa5613`，使用 Revisited 的 ground truth v2。
上游的五类模拟工作系统包含邮件、日历、分析、项目管理和 CRM，另提供公司通讯录工具。
默认加载全部 690 道原题和全部原生工具。Target 需要 Docker，评分使用原生规则。

复制 [配置示例](../examples/workbench_opro.yaml) 为 `experiment.local.yaml`，分别填写
Target 和 Worker 的模型/API。Python 使用 3.12 或更新版本，并安装 Git。
MiniAgent 内置 shell 需要 Linux/WSL/macOS；Claude Code 需允许 Bash，例如
`agent.allowed_tools: [Read, Glob, Grep, Bash]`，其模型连接仍按 Claude Code 的公开配置填写。

```bash
python scripts/run_experiment.py --config experiment.local.yaml --plan
python scripts/run_experiment.py --config experiment.local.yaml --check-only
python scripts/run_experiment.py --config experiment.local.yaml
```

统一入口自动选择 `workbench` 依赖环境，从固定 Git commit 下载/复用原始源码和数据。
缓存遵循 `HF_HOME`；代理、Git 和缓存目录写权限由运行环境提供。
WorkBench 原生 NumPy `<2`；`all` extra 统一使用 NumPy 1.x，WideSearch 的本地评估器也支持该版本。
未使用上游自己的模型循环，MiniAgent / Claude Code 仍原生执行；接入层仅提供工具命令和结果映射。
上游固定日期 `2023-11-30 00:00:00` 作为题目环境信息传入，不替换 harness system prompt。

可选 `benchmark.domains` 筛选原生领域；默认包含 `analytics`、`calendar`、
`customer_relationship_manager`、`email`、`multi_domain`、`project_management`。
原数据没有官方 train/dev/test，必须显式设置 `split_counts` 或 `split`；示例为自定义 1/1/1。
ModularRSI 所需的独立演化数据没有在本接入中提供，不能把同一题库的 heldout 冒充独立来源。

`benchmark.data_path` 可指向完整原生 `data/` 目录，包含 `processed` 下五份数据库 CSV、
`raw/email_addresses.csv` 和 `processed/tasks_and_outcomes/*_tasks_and_outcomes.csv`。
本地数据模式会单独记录路径与内容校验和，不能据此声称默认下载已经验证。

每次任务执行使用独立进程和初始数据库副本。Agent 只获得原题与工具说明，参考操作保留在 host。
执行过的工具调用由 host 记录，再由固定版本的原生评分函数重放并比较数据库最终状态。
主指标 `correct`，附带 `exact_match` 和 `unwanted_side_effects`；不按最终回答文字或模型自报完成评分。
原生 `analytics.create_plot` 只记录模拟图表路径，不真正绘制 PNG，接入层保持该行为。
轨迹末尾保存 `WorkBenchVerifier`，记录 task/data 校验和、源码版本、v2 标记、执行操作及原生分数。
工具错误保留；评分器错误不会被当成正常的答错或通过。

Target 在每题独立的 Docker 容器内通过私有通道调用原生工具。数据库、参考操作和评分器留在宿主机。
数据库线程隔离、原生写操作/副作用评分和公开配置的离线检查，不等于算法 × Target 的真实模型 smoke。
