# ALFWorld 纯文本接入

注册名 `alfworld`，领域为 Interactive Planning。提供原生文本环境接口；不含
视觉 THOR、混合环境或 BUTLER 训练。上游为 [ALFWorld](https://github.com/alfworld/alfworld)，
固定运行依赖 `alfworld==0.4.2`、`textworld[pddl]==1.7.0`。

## 数据与环境

- Linux/WSL/macOS、Python 3.11 或 3.12；Windows 原生启动明确报错。
- 宿主机使用原生 Python 文本环境，Target 需要 Docker。
  TextWorld 编译依赖按[官方说明](https://github.com/microsoft/TextWorld#installation)准备。
- 原生默认来源下 `benchmark.data_path` 必填，指向原始 `json_2.1.1` 或其父目录。
  YAML 路径相对配置文件，CLI 路径相对当前目录。不自动下载任何任务或视觉模型。
- 数据来自官方 release 的 JSON 和 TW-PDDL 游戏包（官方下载脚本中的
  `json_2.1.1_json.zip` 与 `json_2.1.3_tw-pddl.zip`），按其原始路径合并。
  每道任务目录包含 `traj_data.json` 和自包含的 `game.tw-pddl`。
  本适配直接加载现有游戏，不调用生成脚本，不要求视觉检测权重。
  官方 `alfworld-download` 默认还下载 MaskRCNN；本接口不自动调用它。

也可选择 [HF prepared 数据源](hf-prepared-data.md)：不设置 `data_path`，由框架下载约 8 MB 的精选原生任务包并保留 200/140/134 的发布划分。示例见 [hf_prepared_alfworld.yaml](../examples/hf_prepared_alfworld.yaml)。上面的手动数据准备适用于原生默认来源；系统和运行依赖对两种来源都适用。

默认采用框架角色映射：原生 `train → train`、`valid_seen → dev`、
`valid_unseen → test`。后两者仍是官方的 seen/unseen 验证集，并非声称拥有官方隐藏测试集。
`limit` 在保持来源归属的前提下确定性取样；显式 `split_counts` / `split` 会重新划分，
应作为自定义实验协议报告。允许只提供部分原生划分，缺失角色保持为空。

按 `AlfredTWEnv` 的规则排除 movable/Sliced、缺失游戏和未标记可解的游戏。
默认支持全部六类；`task_types` 可指定以下名称的非空子集：

```yaml
task_types:
  - pick_and_place_simple
  - look_at_obj_in_light
  - pick_clean_then_place_in_recep
  - pick_heat_then_place_in_recep
  - pick_cool_then_place_in_recep
  - pick_two_obj_and_place
```

## 执行与评分

每次 rollout 创建独立子进程，通过原生 `textworld.gym.register_game` 与
`AlfredDemangler(shuffle=False)` 加载指定游戏；沿用上游的观察、命令解释及状态转移。
Agent 在自己的工作目录中通过 shell 客户端提交动作。支持 MiniAgent 与启用 Bash
的 Claude Code；不改动两者的原生 system prompt。原生会话留在宿主机，Target 在每题独立的 Docker 容器内通过私有通道提交动作。游戏文件和评分接口留在宿主机。

- `max_steps` 默认 50，是本适配的可配置预算，不宣称复现特定论文设置。
  环境动作预算与 `agent.max_turns`、`agent.timeout` 分别生效。
- `seed` 固定环境随机源与取样；关闭 domain randomization。
- `admissible_commands: false` 默认只给观察；设为 true 时额外返回原生合法动作列表。
  此选项改变可见信息，记录在 metadata 与评分事件中，比较成绩时需保持一致。
- 不启用 expert wrapper；不暴露专家计划、完整 facts、轨迹注释或私有评分接口。
- 主指标 `success` 由宿主侧 `infos["won"]` 映射为 0/1，再由框架求均值。
  `done`、累计奖励或模型文字声明均不能替代成功判定。达到步数上限后停止执行动作。
- 原生执行异常记录为 error，不能伪装成正常的任务失败；Agent 执行错误按框架惯例计零。
  游戏内容校验和写入任务，执行前核验并复制固定内容，结束后释放进程及临时目录。

## 使用与验证范围

示例：[alfworld_opro.yaml](../examples/alfworld_opro.yaml)。先填模型与本地数据路径：

```bash
python scripts/run_experiment.py --config examples/alfworld_opro.yaml --plan
python scripts/run_experiment.py --config examples/alfworld_opro.yaml --check-only
python scripts/run_experiment.py --config examples/alfworld_opro.yaml
```

`--plan` 只输出依赖计划。`--check-only` 安装选定依赖并检查本地任务与配置，不调用模型，
也不能替代真实环境 rollout。缺少数据时明确报错。

当前验证覆盖合成游戏记录和模拟环境下的加载、划分、交互协议、失败传播、成功判定、
动作客户端与进程清理。官方任务、原生 ALFWorld 模拟器及真实模型的端到端运行尚未验证。
