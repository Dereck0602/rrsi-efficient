# ADAS

[English](./adas.md) · **简体中文**

[算法总览](./integrations/algorithms.md) · [示例配置](../examples/adas.yaml)

ADAS 使用设计说明与实测分数组成的档案搜索 Agent 设计。每个新提案从当前 dev 最佳 Snapshot 出发，先修改、复查，再评估。低分设计也留在档案中，但只有严格提高 dev 分数才改变下一轮父版本和最终胜者。Target 活动版本只在搜索结束时提升一次。

## 使用条件

dev 必须非空，不使用 train。Dev 分数进入后续设计提示和版本选择，因此它是搜索集，不是独立最终测试。Runner 随后在 test 上评估选中的 Snapshot。

Worker 必须是独立配置模型和连接的文件编辑 Agent（`algorithm.worker.type: agent`）。设计载体是 Target 声明的资源树，`evolve_scope = {"*"}` 允许编辑全部已声明组件。只有 Target 暴露运行时代码时才能编辑，不会因此开放闭源 harness 内部实现。详见 [Harness 接入](./integrations/harnesses.md)。

## 运行小示例

按[安装指南](./guide/installation.md)准备环境，在 [examples/quickstart.yaml](../examples/quickstart.yaml) 中配置 Target 和 Worker，再从仓库根目录使用其本地数据运行：

```bash
flashrsi run --config examples/quickstart.yaml \
  --algorithm adas \
  --algorithm.worker.type agent \
  --algorithm.worker.name miniagent \
  --algorithm.worker.max_turns 30 \
  --algorithm.max_steps 1 \
  --algorithm.num_reflections 1
```

这会请求一次设计和一次评估前复查。设 `num_reflections: 0` 可省去复查，但该运行就不覆盖反思阶段。追加 `--check-only` 可检查初始化，不调用模型，也不验证远端认证。专用 [ADAS 配置](../examples/adas.yaml)使用两次复查。覆盖方式见[配置与 CLI](./guide/configuration.md)。

## 搜索与选择

1. 在完整 dev 上评估初始 Snapshot，用其说明与分数初始化档案，并设为当前最佳。
2. 检出最佳 Snapshot，向 Worker 提供评分档案和 Target 组件规则，请它编辑文件并用 `<DESIGN>` 说明新设计。
3. 在同一 checkout 中再执行 `num_reflections` 次编辑复查。每次读取最新设计说明和组件规则，只有最初的提案提示显式包含档案。这些复查都在测量前进行，框架不做中间评估。
4. 清理已知运行时产物，只提交一次。空编辑或非法资源变化被拒绝，不评估候选，但消耗一步。
5. 在完整 dev 上评估合法候选，无论升降都将最终说明与分数加入档案。严格更高分才更新 `best`，同分保留此前版本。
6. 下一步仍从 best 出发，而不是从落选的上一个候选出发。结束时提升 best，记录档案和被拒设计。

传给 Worker 的档案包含 `account` 和 `score`，不包含全部历史源码树或执行轨迹。完整列表会保留，但进入提示的序列化内容受 `max_evidence_chars` 截断。与 [Meta-Harness](./meta-harness.zh-CN.md) 不同，这里不提供可选择检索的文件系统历史。

## 设计说明与非法编辑

最后一对完整 `<DESIGN>...</DESIGN>` 内的文本作为设计说明；没有完整标签时使用整条回复。这里没有 AHE 式逐文件预测 manifest，编辑后的文件定义候选，说明供后续提案理解设计。

被拒编辑没有分数，所以不进入评分档案。拒绝理由写入决策及最终 `declined_designs`，但不交给下一轮提案提示。已经评估的低分设计则会进入档案。

Worker 失败向外传播。候选运行错误由 Target 和 Benchmark 的契约处理，没有 ADAS 专用修复循环。原生输出格式错误或执行接口损坏仍可能抛异常并终止搜索，不能保证所有坏设计都会变成零分档案条目。

## 参数与成本

实现默认值放在 `algorithm` 下。

| 字段 | 默认值 | 含义 |
| --- | --- | --- |
| `max_steps` | `10` | 设计尝试数；整数，至少 1，非法尝试也计数。 |
| `num_reflections` | `2` | 同一 checkout 中评估前的额外编辑复查次数；整数，至少 0。 |
| `max_evidence_chars` | `30000` | 每份序列化证据字符上限，档案与规则分别限制；至少 1。 |

设 dev 有 D 题、最多 S 次尝试，Target 搜索最多执行 `(1 + S) * D` 次任务，含初始版本。每次正常完成的尝试调用 `1 + num_reflections` 次 Worker 编辑。非法编辑跳过候选 dev 评估，但仍消耗编辑调用。原生模型/工具轮次、后端重试和最终 test 另计。

## 查看实验记录

`kind: admit` 表示已测设计进入档案，其中 `accepted` 专门表示它是否成为新最佳/下一轮父版本。结合 `parent_id`、`candidate_id`、`parent_score`、`candidate_score` 和 `account` 区分探索与选中。

`kind: decline` 解释未评估的尝试。最终 `promote` 包含选中的 `candidate_id`、完整 `archive` 和 `declined_designs`。档案行本身只有说明与分数，Snapshot 对应关系在各次尝试的决策中。

## 来源与适配

[搜索实现](../flashrsi/algorithms/adas/__init__.py)和[提示](../flashrsi/algorithms/adas/_prompts.py)引用 `ShengranHu/ADAS`，但本仓库**仍未固定上游 commit**。这是来源尚待补齐的机制移植，不是已核验到具体版本的复现。

原始固定框架中的 `forward()` 设计在这里由原生 harness 文件表达。档案从一个 Target Snapshot 起步，不使用多个预定义 Agent 架构。反思在同一 checkout 编辑，说明通过回复传递，组件规则限制设计，最终按 FlashRSI dev 分数选择。未实现独立 Agent DSL、多种子架构库或跨进程搜索恢复。
