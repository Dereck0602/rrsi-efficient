<p align="center">
  <img src="assets/readme/cover.gif" alt="FlashRSI 动态封面：深黑背景上，蓝白电弧沿左侧闪电和右侧大号白色字标流动。" width="100%">
</p>

<h1 align="center">FlashRSI: A Unified Framework for Agent Self-Improvement</h1>

<p align="center"><i>一键在不同 Agent 与任务上复现、比较和开发 RSI 方法。</i></p>

<p align="center">
  FlashRSI 集成 <b>13 种 RSI 算法</b>、<b>30 个 benchmark</b> 和 <b>7 个 harness</b>（MiniAgent、Claude Code、Codex、Pi、Hermes、OpenClaw、OpenCode），用于复现、比较和开发 Agent 自我改进方法。
</p>

<p align="center">
  <a href="https://arxiv.org/search/?query=FlashRSI&amp;searchtype=all"><img src="https://img.shields.io/badge/Paper-arXiv-B31B1B?style=for-the-badge&amp;logo=arxiv&amp;logoColor=white" alt="Search arXiv for FlashRSI"></a>
  <a href="https://huggingface.co/datasets/RUC-NLPIR/FlashRSI"><img src="https://img.shields.io/badge/Datasets-Hugging%20Face-FF8C00?style=for-the-badge&amp;logo=huggingface&amp;logoColor=white" alt="FlashRSI datasets on Hugging Face"></a>
  <a href="https://deepwiki.com/RUC-NLPIR/FlashRSI"><img src="https://img.shields.io/badge/Ask%20DeepWiki-8B5CF6?style=for-the-badge" alt="Ask DeepWiki about FlashRSI"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache--2.0-D22128?style=for-the-badge&amp;logo=apache&amp;logoColor=white" alt="Apache-2.0 License"></a><br>
  <a href="#demo"><img src="https://img.shields.io/badge/Live_Demo-Watch-FF5C8A?style=for-the-badge&amp;logo=githubpages&amp;logoColor=white" alt="Watch the live demo"></a>
  <a href="https://ruc-nlpir.github.io/FlashRSI/project.html"><img src="https://img.shields.io/badge/Project_Page-Visit-0E9B9B?style=for-the-badge&amp;logo=githubpages&amp;logoColor=white" alt="FlashRSI project page"></a>
  <a href="https://ruc-nlpir.github.io/FlashRSI/usage.zh-CN.html"><img src="https://img.shields.io/badge/Docs-Read_the_guide-526CFE?style=for-the-badge&amp;logo=readthedocs&amp;logoColor=white" alt="FlashRSI documentation"></a>
</p>

<p align="center">
  <a href="README.md">English</a> · <b>简体中文</b>
</p>

<p align="center">
  <a href="#主要特性">主要特性</a> &nbsp;/&nbsp;
  <a href="#快速开始">快速开始</a> &nbsp;/&nbsp;
  <a href="#支持的算法">支持算法</a> &nbsp;/&nbsp;
  <a href="#整体实验结果">整体实验结果</a> &nbsp;/&nbsp;
  <a href="#支持的-harnesses">Harnesses</a> &nbsp;/&nbsp;
  <a href="#支持的-benchmarks">Benchmarks</a> &nbsp;/&nbsp;
</p>

<p align="center">
  <a href="assets/readme/overall-result-1.jpg">
    <img src="assets/readme/overall-result-1.jpg" alt="Claude Code 在 source、近域和远 OOD benchmark 上的最终分数及相对初始 Harness 变化。" width="100%">
  </a>
</p>

<p align="center">
  <a href="assets/readme/overall-result-2.jpg">
    <img src="assets/readme/overall-result-2.jpg" alt="Mini-Agent 在 source、近域和远 OOD benchmark 上的最终分数及相对初始 Harness 变化。" width="100%">
  </a>
</p>

<a id="demo"></a>
<p align="center">
  <img src="assets/readme/slot-machine.gif" alt="Benchmark、RSI 算法和 harness 三个转轮停在一组组合，随后绘制 Training、Held-out、Near-domain 和 OOD-domain 示意曲线。" width="100%">
</p>

## 主要特性

- 🧩 **可配置实验框架。** 在同一个 YAML/CLI 实验中组合 RSI 算法、Target harness、Worker、benchmark
  和各自的模型配置；资源范围与执行角色可以直接切换。
- 🧪 **30 个 Benchmark 与 13 种 RSI 方法。** 覆盖问答、推理、数学、代码、网页研究、文档、工具调用
  和交互任务，提供 **29 个套件的 34 份 Hugging Face 固定配置**，以及 OPRO、GEPA、EvoPrompt、ACE、
  SkillOpt、Evo-Harness、AHE、ADAS、DarwinX、Meta-Harness、ModularRSI、RRSI、Arbor 的可运行适配。
- 🧠 **原生 Harness 与可复现证据。** 支持 MiniAgent、Claude Code、Codex、Pi、Hermes、OpenClaw 和 OpenCode，将 prompts、skills、工具、
  配置、hooks、plugins、memory 与 runtime 资源纳入版本化 Snapshot；每次运行集中保存候选版本、轨迹、
  指标、用量和任务产物。
- 🚀 **简单工作流与完整文档。** 使用 `flashrsi run` 演化、`flashrsi evaluate` 评估 Snapshot，配套方法
  示例、可搜索文档网站和 GitHub Pages 支持；OpenCode 通过纯 Python Snapshot runtime 执行，
  不依赖 Bun、Node、npm 或 OpenCode CLI。

[使用文档 →](docs/usage.zh-CN.md)

<p align="center">
  <a href="assets/readme/framework-overview.jpg">
    <img src="assets/readme/framework-overview.jpg" alt="FlashRSI 框架概览：12 种 RSI 算法、不可变 AgentSnapshot、5 个 Agent harness 和 30 个 benchmark。" width="100%">
  </a>
</p>

## 快速开始

### 1. 安装

推荐 Linux / WSL + Python 3.12。

```bash
git clone https://github.com/RUC-NLPIR/FlashRSI.git
cd FlashRSI
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[all]"
```

需要运行 Harbor 任务时，再安装 Harbor extra：

```bash
python -m pip install -e ".[all,harbor]"
```

τ²-bench 由一键实验入口按固定版本单独安装，不能和 Harbor 放在同一个环境中。

### 2. 一键运行

使用仓库自带的 12 道 QA 题目，运行 **SkillOpt × SearchQA × MiniAgent**。

**命令行**

先在 shell 中设置 `TARGET_API_KEY` 和 `WORKER_API_KEY`，分别对应 Target 和 Worker；两者互不继承。自定义端点还需分别设置各角色的 `llm.base_url`。

```bash
flashrsi run \
  --config examples/quickstart.yaml \
  --algorithm skillopt \
  --benchmark searchqa \
  --agent-harness miniagent \
  --model gpt-5-mini \
  --worker-model gpt-5-mini \
  --agent.llm.api_key "$TARGET_API_KEY" \
  --algorithm.worker.llm.api_key "$WORKER_API_KEY"
```

**[YAML](examples/quickstart.yaml)**

在 [examples/quickstart.yaml](examples/quickstart.yaml) 中分别填写 `agent.llm` 和 `algorithm.worker.llm` 的模型、密钥及可选端点，再运行：

```bash
flashrsi run --config examples/quickstart.yaml
```

结果保存在 `runs/`，见[分数、Snapshot 与轨迹说明](docs/running-experiments.md#read-the-results)。自动准备环境见[一键实验指南](docs/experiment-launcher.zh-CN.md)。

## 支持的算法

FlashRSI 目前实现 13 种 RSI 方法：

| 方法 | Scope | 搜索组织 | 使用的反馈 |
| --- | --- | --- | --- |
| [OPRO](https://arxiv.org/abs/2309.03409) | 指令搜索 | 评分历史 · 反思 · 候选群体 | 训练分数、输出 |
| [GEPA](https://arxiv.org/abs/2507.19457) | 指令搜索 | 候选池与反思 | 分数、轨迹 |
| [EvoPrompt](https://arxiv.org/abs/2309.08532) | 指令搜索 | GA / DE 候选群体 | 开发集分数 |
| [SkillOpt](https://arxiv.org/abs/2605.23904) | Skill 演化 | 快速 / 慢速更新 | 训练轨迹、开发集分数 |
| [Evo-Harness](https://arxiv.org/abs/2608.15071v2) | Skill 演化 | 任务流积累 | 失败轨迹 |
| [ACE](https://arxiv.org/abs/2510.04618) | 上下文演化 | Playbook 积累 | 任务反馈、反思 |
| [AHE](https://arxiv.org/abs/2604.25850) | 迭代式 Harness 优化 | 连续编辑 | 轨迹、诊断、编辑审计 |
| [Meta-Harness](https://arxiv.org/abs/2603.28052) | 迭代式 Harness 优化 | 基于文件的历史 | 代码、分数、轨迹 |
| [RRSI](https://arxiv.org/abs/2609.24972) | 迭代式 Harness 优化 | 历史引导的修订 | 轨迹、分数、Token 成本 |
| [ADAS](https://arxiv.org/abs/2408.08435) | 归档搜索 | 设计归档 | 设计记录、开发集分数 |
| [DarwinX](https://arxiv.org/abs/2608.07545) | 分支演化 | 分支与重组 | 重复结果、轨迹 |
| [ModularRSI](https://arxiv.org/abs/2609.14857) | 分支演化 | 独立分支；集成 | 成功 / 失败轨迹 |
| [Arbor](docs/arbor.md) | 分支演化 | 假设树与经验向上归纳 | Train 轨迹、dev 分数 |

Arbor 为新接入算法；原框架图和下方实验表仍对应十二方法的已有研究。已有一次小规模 Arbor × Codex × WorkBench smoke，覆盖与限制见 [Arbor 验证说明](docs/arbor.md#真实模型验证)，该记录不代表性能收益。

SkillOpt 的快慢更新、选优规则和参数预算见 [SkillOpt 使用说明](docs/skillopt.zh-CN.md)。

指令搜索方法的使用说明见 [OPRO](docs/opro.zh-CN.md)、[GEPA](docs/gepa.zh-CN.md) 和 [EvoPrompt](docs/evoprompt.zh-CN.md)，分别介绍其反馈来源、选优规则和预算单位。

经验积累方法见 [ACE](docs/ace.zh-CN.md) 的 playbook 策展与 offline/online 选优，以及 [Evo-Harness](docs/evo-harness.zh-CN.md) 的任务流原生技能编译。

目录编辑方法见 [AHE](docs/ahe.zh-CN.md)、[ADAS](docs/adas.zh-CN.md) 和 [Meta-Harness](docs/meta-harness.zh-CN.md)，分别说明编辑基线、证据来源与最终选择规则。

这些方法均适配了 FlashRSI 的资源与评估接口。Evo-Harness 和 Harness 资源编辑方法需要 Agent Worker；具体条件见[选型与运行指南](docs/running-experiments.md#choose-an-algorithm-and-harness)。

### 整体实验结果

下表汇总了 12 种方法在 Claude Code 和 Mini-Agent 上的 source-task 表现，以及冻结后的迁移表现，
覆盖 4 个 source benchmark、5 个近域迁移 benchmark 和 2 个远 OOD benchmark。分数均为百分比；
每个结果单元格先显示最终分数，再显示相对对应初始 Harness 的百分点变化，粗体表示每个 benchmark 与 Harness
组合中的最高最终分数（包括并列）。

<table>
  <thead>
    <tr>
      <th rowspan="2" align="left">方法</th>
      <th rowspan="2" align="left">目标 Harness</th>
      <th colspan="4" align="center">Source<br><sub>域内</sub></th>
      <th colspan="5" align="center">近域<br><sub>冻结迁移</sub></th>
      <th colspan="2" align="center">远 OOD<br><sub>冻结迁移</sub></th>
    </tr>
    <tr>
      <th align="center">SearchQA<br><sub>域内</sub></th>
      <th align="center">HotpotQA<br><sub>域内</sub></th>
      <th align="center">Terminal-Bench 2.0<br><sub>域内</sub></th>
      <th align="center">AppWorld<br><sub>域内</sub></th>
      <th align="center">SearchQA → NQ<br><sub>近域</sub></th>
      <th align="center">SearchQA → TriviaQA<br><sub>近域</sub></th>
      <th align="center">HotpotQA → MuSiQue<br><sub>近域</sub></th>
      <th align="center">Terminal-Bench 2.0 → SWE-bench<br><sub>近域</sub></th>
      <th align="center">AppWorld → BFCL Multi-Turn<br><sub>近域</sub></th>
      <th align="center">SearchQA → OfficeQA Pro<br><sub>远 OOD</sub></th>
      <th align="center">HotpotQA → DeepSearchQA<br><sub>远 OOD</sub></th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td rowspan="2" align="left"><strong>初始 Harness</strong></td>
      <td align="left">Claude Code</td>
      <td align="center">73.93</td>
      <td align="center">57.32</td>
      <td align="center">63.33</td>
      <td align="center">64.88</td>
      <td align="center">56.67</td>
      <td align="center">68.67</td>
      <td align="center">67.50</td>
      <td align="center">76.67</td>
      <td align="center">65.00</td>
      <td align="center">44.19</td>
      <td align="center">66.00</td>
    </tr>
    <tr>
      <td align="left">Mini-Agent</td>
      <td align="center">76.43</td>
      <td align="center">58.54</td>
      <td align="center">66.67</td>
      <td align="center">65.48</td>
      <td align="center">59.33</td>
      <td align="center">74.33</td>
      <td align="center">65.83</td>
      <td align="center">80.00</td>
      <td align="center">70.00</td>
      <td align="center">27.91</td>
      <td align="center">52.00</td>
    </tr>
    <tr>
      <td rowspan="2" align="left"><strong>SkillOpt</strong></td>
      <td align="left">Claude Code</td>
      <td align="center">83.14<br><sub>+9.21 pp</sub></td>
      <td align="center">60.98<br><sub>+3.66 pp</sub></td>
      <td align="center">70.00<br><sub>+6.67 pp</sub></td>
      <td align="center">84.52<br><sub>+19.64 pp</sub></td>
      <td align="center">69.00<br><sub>+12.33 pp</sub></td>
      <td align="center">82.00<br><sub>+13.33 pp</sub></td>
      <td align="center">76.67<br><sub>+9.17 pp</sub></td>
      <td align="center">80.00<br><sub>+3.33 pp</sub></td>
      <td align="center">68.75<br><sub>+3.75 pp</sub></td>
      <td align="center">46.51<br><sub>+2.32 pp</sub></td>
      <td align="center">58.00<br><sub>-8.00 pp</sub></td>
    </tr>
    <tr>
      <td align="left">Mini-Agent</td>
      <td align="center">82.93<br><sub>+6.50 pp</sub></td>
      <td align="center">59.76<br><sub>+1.22 pp</sub></td>
      <td align="center">66.67<br><sub>0.00 pp</sub></td>
      <td align="center"><strong>91.67</strong><br><sub>+26.19 pp</sub></td>
      <td align="center">62.00<br><sub>+2.67 pp</sub></td>
      <td align="center">84.33<br><sub>+10.00 pp</sub></td>
      <td align="center">74.17<br><sub>+8.34 pp</sub></td>
      <td align="center"><strong>86.67</strong><br><sub>+6.67 pp</sub></td>
      <td align="center">68.75<br><sub>-1.25 pp</sub></td>
      <td align="center">37.21<br><sub>+9.30 pp</sub></td>
      <td align="center">46.00<br><sub>-6.00 pp</sub></td>
    </tr>
    <tr>
      <td rowspan="2" align="left"><strong>OPRO</strong></td>
      <td align="left">Claude Code</td>
      <td align="center">81.43<br><sub>+7.50 pp</sub></td>
      <td align="center">62.20<br><sub>+4.88 pp</sub></td>
      <td align="center">63.33<br><sub>0.00 pp</sub></td>
      <td align="center">66.07<br><sub>+1.19 pp</sub></td>
      <td align="center"><strong>69.67</strong><br><sub>+13.00 pp</sub></td>
      <td align="center">80.33<br><sub>+11.66 pp</sub></td>
      <td align="center">73.33<br><sub>+5.83 pp</sub></td>
      <td align="center">80.00<br><sub>+3.33 pp</sub></td>
      <td align="center">66.25<br><sub>+1.25 pp</sub></td>
      <td align="center">51.16<br><sub>+6.97 pp</sub></td>
      <td align="center">60.00<br><sub>-6.00 pp</sub></td>
    </tr>
    <tr>
      <td align="left">Mini-Agent</td>
      <td align="center">80.71<br><sub>+4.28 pp</sub></td>
      <td align="center">60.98<br><sub>+2.44 pp</sub></td>
      <td align="center">66.67<br><sub>0.00 pp</sub></td>
      <td align="center">59.52<br><sub>-5.96 pp</sub></td>
      <td align="center">64.67<br><sub>+5.34 pp</sub></td>
      <td align="center">81.33<br><sub>+7.00 pp</sub></td>
      <td align="center"><strong>78.33</strong><br><sub>+12.50 pp</sub></td>
      <td align="center">80.00<br><sub>0.00 pp</sub></td>
      <td align="center">68.75<br><sub>-1.25 pp</sub></td>
      <td align="center">41.86<br><sub>+13.95 pp</sub></td>
      <td align="center">44.00<br><sub>-8.00 pp</sub></td>
    </tr>
    <tr>
      <td rowspan="2" align="left"><strong>AHE</strong></td>
      <td align="left">Claude Code</td>
      <td align="center">79.36<br><sub>+5.43 pp</sub></td>
      <td align="center">60.98<br><sub>+3.66 pp</sub></td>
      <td align="center">70.00<br><sub>+6.67 pp</sub></td>
      <td align="center">91.67<br><sub>+26.79 pp</sub></td>
      <td align="center">66.00<br><sub>+9.33 pp</sub></td>
      <td align="center">81.67<br><sub>+13.00 pp</sub></td>
      <td align="center">74.17<br><sub>+6.67 pp</sub></td>
      <td align="center">80.00<br><sub>+3.33 pp</sub></td>
      <td align="center">71.25<br><sub>+6.25 pp</sub></td>
      <td align="center">46.51<br><sub>+2.32 pp</sub></td>
      <td align="center">60.00<br><sub>-6.00 pp</sub></td>
    </tr>
    <tr>
      <td align="left">Mini-Agent</td>
      <td align="center">78.57<br><sub>+2.14 pp</sub></td>
      <td align="center">57.32<br><sub>-1.22 pp</sub></td>
      <td align="center">60.00<br><sub>-6.67 pp</sub></td>
      <td align="center">71.43<br><sub>+5.95 pp</sub></td>
      <td align="center">66.33<br><sub>+7.00 pp</sub></td>
      <td align="center">79.00<br><sub>+4.67 pp</sub></td>
      <td align="center">73.33<br><sub>+7.50 pp</sub></td>
      <td align="center">83.33<br><sub>+3.33 pp</sub></td>
      <td align="center">72.50<br><sub>+2.50 pp</sub></td>
      <td align="center">34.88<br><sub>+6.97 pp</sub></td>
      <td align="center">46.00<br><sub>-6.00 pp</sub></td>
    </tr>
    <tr>
      <td rowspan="2" align="left"><strong>ACE</strong></td>
      <td align="left">Claude Code</td>
      <td align="center">83.93<br><sub>+10.00 pp</sub></td>
      <td align="center"><strong>63.41</strong><br><sub>+6.09 pp</sub></td>
      <td align="center">66.67<br><sub>+3.34 pp</sub></td>
      <td align="center">91.07<br><sub>+26.19 pp</sub></td>
      <td align="center">64.00<br><sub>+7.33 pp</sub></td>
      <td align="center">85.67<br><sub>+17.00 pp</sub></td>
      <td align="center">72.50<br><sub>+5.00 pp</sub></td>
      <td align="center">73.33<br><sub>-3.34 pp</sub></td>
      <td align="center"><strong>73.75</strong><br><sub>+8.75 pp</sub></td>
      <td align="center">41.86<br><sub>-2.33 pp</sub></td>
      <td align="center">54.00<br><sub>-12.00 pp</sub></td>
    </tr>
    <tr>
      <td align="left">Mini-Agent</td>
      <td align="center">84.71<br><sub>+8.28 pp</sub></td>
      <td align="center"><strong>63.41</strong><br><sub>+4.87 pp</sub></td>
      <td align="center">70.00<br><sub>+3.33 pp</sub></td>
      <td align="center"><strong>91.67</strong><br><sub>+26.19 pp</sub></td>
      <td align="center">62.67<br><sub>+3.34 pp</sub></td>
      <td align="center">85.33<br><sub>+11.00 pp</sub></td>
      <td align="center">75.83<br><sub>+10.00 pp</sub></td>
      <td align="center">70.00<br><sub>-10.00 pp</sub></td>
      <td align="center">67.50<br><sub>-2.50 pp</sub></td>
      <td align="center">30.23<br><sub>+2.32 pp</sub></td>
      <td align="center">54.00<br><sub>+2.00 pp</sub></td>
    </tr>
    <tr>
      <td rowspan="2" align="left"><strong>EvoPrompt</strong></td>
      <td align="left">Claude Code</td>
      <td align="center">74.07<br><sub>+0.14 pp</sub></td>
      <td align="center">58.54<br><sub>+1.22 pp</sub></td>
      <td align="center">63.33<br><sub>0.00 pp</sub></td>
      <td align="center">61.90<br><sub>-2.98 pp</sub></td>
      <td align="center">59.67<br><sub>+3.00 pp</sub></td>
      <td align="center">69.00<br><sub>+0.33 pp</sub></td>
      <td align="center">70.00<br><sub>+2.50 pp</sub></td>
      <td align="center"><strong>83.33</strong><br><sub>+6.66 pp</sub></td>
      <td align="center">66.25<br><sub>+1.25 pp</sub></td>
      <td align="center">44.19<br><sub>0.00 pp</sub></td>
      <td align="center">64.00<br><sub>-2.00 pp</sub></td>
    </tr>
    <tr>
      <td align="left">Mini-Agent</td>
      <td align="center">76.71<br><sub>+0.28 pp</sub></td>
      <td align="center">59.76<br><sub>+1.22 pp</sub></td>
      <td align="center"><strong>76.67</strong><br><sub>+10.00 pp</sub></td>
      <td align="center">64.29<br><sub>-1.19 pp</sub></td>
      <td align="center">58.67<br><sub>-0.66 pp</sub></td>
      <td align="center">74.00<br><sub>-0.33 pp</sub></td>
      <td align="center">67.50<br><sub>+1.67 pp</sub></td>
      <td align="center"><strong>86.67</strong><br><sub>+6.67 pp</sub></td>
      <td align="center">63.75<br><sub>-6.25 pp</sub></td>
      <td align="center">34.88<br><sub>+6.97 pp</sub></td>
      <td align="center">40.00<br><sub>-12.00 pp</sub></td>
    </tr>
    <tr>
      <td rowspan="2" align="left"><strong>GEPA</strong></td>
      <td align="left">Claude Code</td>
      <td align="center">82.43<br><sub>+8.50 pp</sub></td>
      <td align="center">62.20<br><sub>+4.88 pp</sub></td>
      <td align="center">66.67<br><sub>+3.34 pp</sub></td>
      <td align="center">67.86<br><sub>+2.98 pp</sub></td>
      <td align="center">65.33<br><sub>+8.66 pp</sub></td>
      <td align="center">83.67<br><sub>+15.00 pp</sub></td>
      <td align="center">78.33<br><sub>+10.83 pp</sub></td>
      <td align="center">76.67<br><sub>0.00 pp</sub></td>
      <td align="center">70.00<br><sub>+5.00 pp</sub></td>
      <td align="center">41.86<br><sub>-2.33 pp</sub></td>
      <td align="center">52.00<br><sub>-14.00 pp</sub></td>
    </tr>
    <tr>
      <td align="left">Mini-Agent</td>
      <td align="center">84.64<br><sub>+8.21 pp</sub></td>
      <td align="center">59.76<br><sub>+1.22 pp</sub></td>
      <td align="center">70.00<br><sub>+3.33 pp</sub></td>
      <td align="center">75.00<br><sub>+9.52 pp</sub></td>
      <td align="center">61.67<br><sub>+2.34 pp</sub></td>
      <td align="center"><strong>86.33</strong><br><sub>+12.00 pp</sub></td>
      <td align="center">75.83<br><sub>+10.00 pp</sub></td>
      <td align="center">70.00<br><sub>-10.00 pp</sub></td>
      <td align="center">62.50<br><sub>-7.50 pp</sub></td>
      <td align="center">51.16<br><sub>+23.25 pp</sub></td>
      <td align="center">34.00<br><sub>-18.00 pp</sub></td>
    </tr>
    <tr>
      <td rowspan="2" align="left"><strong>ADAS</strong></td>
      <td align="left">Claude Code</td>
      <td align="center">81.36<br><sub>+7.43 pp</sub></td>
      <td align="center">60.98<br><sub>+3.66 pp</sub></td>
      <td align="center">73.33<br><sub>+10.00 pp</sub></td>
      <td align="center">89.88<br><sub>+25.00 pp</sub></td>
      <td align="center">63.67<br><sub>+7.00 pp</sub></td>
      <td align="center">83.33<br><sub>+14.66 pp</sub></td>
      <td align="center">77.50<br><sub>+10.00 pp</sub></td>
      <td align="center">73.33<br><sub>-3.34 pp</sub></td>
      <td align="center">70.00<br><sub>+5.00 pp</sub></td>
      <td align="center">51.16<br><sub>+6.97 pp</sub></td>
      <td align="center">54.00<br><sub>-12.00 pp</sub></td>
    </tr>
    <tr>
      <td align="left">Mini-Agent</td>
      <td align="center"><strong>84.86</strong><br><sub>+8.43 pp</sub></td>
      <td align="center">60.98<br><sub>+2.44 pp</sub></td>
      <td align="center">66.67<br><sub>0.00 pp</sub></td>
      <td align="center">79.76<br><sub>+14.28 pp</sub></td>
      <td align="center">62.33<br><sub>+3.00 pp</sub></td>
      <td align="center">84.33<br><sub>+10.00 pp</sub></td>
      <td align="center">74.17<br><sub>+8.34 pp</sub></td>
      <td align="center">73.33<br><sub>-6.67 pp</sub></td>
      <td align="center">70.00<br><sub>0.00 pp</sub></td>
      <td align="center">32.56<br><sub>+4.65 pp</sub></td>
      <td align="center">34.00<br><sub>-18.00 pp</sub></td>
    </tr>
    <tr>
      <td rowspan="2" align="left"><strong>DarwinX</strong></td>
      <td align="left">Claude Code</td>
      <td align="center">80.50<br><sub>+6.57 pp</sub></td>
      <td align="center">60.98<br><sub>+3.66 pp</sub></td>
      <td align="center">63.33<br><sub>0.00 pp</sub></td>
      <td align="center">70.83<br><sub>+5.95 pp</sub></td>
      <td align="center">65.67<br><sub>+9.00 pp</sub></td>
      <td align="center">82.00<br><sub>+13.33 pp</sub></td>
      <td align="center">77.50<br><sub>+10.00 pp</sub></td>
      <td align="center">66.67<br><sub>-10.00 pp</sub></td>
      <td align="center">72.50<br><sub>+7.50 pp</sub></td>
      <td align="center">48.84<br><sub>+4.65 pp</sub></td>
      <td align="center">60.00<br><sub>-6.00 pp</sub></td>
    </tr>
    <tr>
      <td align="left">Mini-Agent</td>
      <td align="center">79.29<br><sub>+2.86 pp</sub></td>
      <td align="center">60.98<br><sub>+2.44 pp</sub></td>
      <td align="center">66.67<br><sub>0.00 pp</sub></td>
      <td align="center">69.05<br><sub>+3.57 pp</sub></td>
      <td align="center">64.67<br><sub>+5.34 pp</sub></td>
      <td align="center">83.00<br><sub>+8.67 pp</sub></td>
      <td align="center">75.83<br><sub>+10.00 pp</sub></td>
      <td align="center">83.33<br><sub>+3.33 pp</sub></td>
      <td align="center">67.50<br><sub>-2.50 pp</sub></td>
      <td align="center">41.86<br><sub>+13.95 pp</sub></td>
      <td align="center">34.00<br><sub>-18.00 pp</sub></td>
    </tr>
    <tr>
      <td rowspan="2" align="left"><strong>Meta-Harness</strong></td>
      <td align="left">Claude Code</td>
      <td align="center"><strong>85.07</strong><br><sub>+11.14 pp</sub></td>
      <td align="center">62.20<br><sub>+4.88 pp</sub></td>
      <td align="center">70.00<br><sub>+6.67 pp</sub></td>
      <td align="center">90.48<br><sub>+25.60 pp</sub></td>
      <td align="center">64.00<br><sub>+7.33 pp</sub></td>
      <td align="center"><strong>86.33</strong><br><sub>+17.66 pp</sub></td>
      <td align="center"><strong>80.00</strong><br><sub>+12.50 pp</sub></td>
      <td align="center">80.00<br><sub>+3.33 pp</sub></td>
      <td align="center">66.25<br><sub>+1.25 pp</sub></td>
      <td align="center"><strong>53.49</strong><br><sub>+9.30 pp</sub></td>
      <td align="center"><strong>68.00</strong><br><sub>+2.00 pp</sub></td>
    </tr>
    <tr>
      <td align="left">Mini-Agent</td>
      <td align="center">79.14<br><sub>+2.71 pp</sub></td>
      <td align="center">59.76<br><sub>+1.22 pp</sub></td>
      <td align="center">73.33<br><sub>+6.66 pp</sub></td>
      <td align="center">88.10<br><sub>+22.62 pp</sub></td>
      <td align="center">65.67<br><sub>+6.34 pp</sub></td>
      <td align="center">81.00<br><sub>+6.67 pp</sub></td>
      <td align="center">72.50<br><sub>+6.67 pp</sub></td>
      <td align="center">76.67<br><sub>-3.33 pp</sub></td>
      <td align="center">73.75<br><sub>+3.75 pp</sub></td>
      <td align="center">41.86<br><sub>+13.95 pp</sub></td>
      <td align="center">38.00<br><sub>-14.00 pp</sub></td>
    </tr>
    <tr>
      <td rowspan="2" align="left"><strong>ModularRSI</strong></td>
      <td align="left">Claude Code</td>
      <td align="center">81.07<br><sub>+7.14 pp</sub></td>
      <td align="center">59.76<br><sub>+2.44 pp</sub></td>
      <td align="center">60.00<br><sub>-3.33 pp</sub></td>
      <td align="center">56.55<br><sub>-8.33 pp</sub></td>
      <td align="center">63.00<br><sub>+6.33 pp</sub></td>
      <td align="center">84.00<br><sub>+15.33 pp</sub></td>
      <td align="center">72.50<br><sub>+5.00 pp</sub></td>
      <td align="center">80.00<br><sub>+3.33 pp</sub></td>
      <td align="center">65.00<br><sub>0.00 pp</sub></td>
      <td align="center">44.19<br><sub>0.00 pp</sub></td>
      <td align="center">52.00<br><sub>-14.00 pp</sub></td>
    </tr>
    <tr>
      <td align="left">Mini-Agent</td>
      <td align="center">78.64<br><sub>+2.21 pp</sub></td>
      <td align="center">59.76<br><sub>+1.22 pp</sub></td>
      <td align="center">70.00<br><sub>+3.33 pp</sub></td>
      <td align="center">69.05<br><sub>+3.57 pp</sub></td>
      <td align="center">67.00<br><sub>+7.67 pp</sub></td>
      <td align="center">81.00<br><sub>+6.67 pp</sub></td>
      <td align="center">65.83<br><sub>0.00 pp</sub></td>
      <td align="center">73.33<br><sub>-6.67 pp</sub></td>
      <td align="center"><strong>75.00</strong><br><sub>+5.00 pp</sub></td>
      <td align="center"><strong>53.49</strong><br><sub>+25.58 pp</sub></td>
      <td align="center">40.00<br><sub>-12.00 pp</sub></td>
    </tr>
    <tr>
      <td rowspan="2" align="left"><strong>RRSI</strong></td>
      <td align="left">Claude Code</td>
      <td align="center">82.79<br><sub>+8.86 pp</sub></td>
      <td align="center">60.98<br><sub>+3.66 pp</sub></td>
      <td align="center"><strong>76.67</strong><br><sub>+13.34 pp</sub></td>
      <td align="center"><strong>92.86</strong><br><sub>+27.98 pp</sub></td>
      <td align="center">64.00<br><sub>+7.33 pp</sub></td>
      <td align="center">83.67<br><sub>+15.00 pp</sub></td>
      <td align="center">79.17<br><sub>+11.67 pp</sub></td>
      <td align="center">80.00<br><sub>+3.33 pp</sub></td>
      <td align="center">72.50<br><sub>+7.50 pp</sub></td>
      <td align="center"><strong>53.49</strong><br><sub>+9.30 pp</sub></td>
      <td align="center">58.00<br><sub>-8.00 pp</sub></td>
    </tr>
    <tr>
      <td align="left">Mini-Agent</td>
      <td align="center">81.64<br><sub>+5.21 pp</sub></td>
      <td align="center">59.76<br><sub>+1.22 pp</sub></td>
      <td align="center"><strong>76.67</strong><br><sub>+10.00 pp</sub></td>
      <td align="center">84.52<br><sub>+19.04 pp</sub></td>
      <td align="center"><strong>68.67</strong><br><sub>+9.34 pp</sub></td>
      <td align="center">83.00<br><sub>+8.67 pp</sub></td>
      <td align="center">76.67<br><sub>+10.84 pp</sub></td>
      <td align="center">76.67<br><sub>-3.33 pp</sub></td>
      <td align="center">66.25<br><sub>-3.75 pp</sub></td>
      <td align="center"><strong>53.49</strong><br><sub>+25.58 pp</sub></td>
      <td align="center">36.00<br><sub>-16.00 pp</sub></td>
    </tr>
    <tr>
      <td rowspan="2" align="left"><strong>Evo-Harness</strong></td>
      <td align="left">Claude Code</td>
      <td align="center">75.36<br><sub>+1.43 pp</sub></td>
      <td align="center">60.98<br><sub>+3.66 pp</sub></td>
      <td align="center">66.67<br><sub>+3.34 pp</sub></td>
      <td align="center">81.55<br><sub>+16.67 pp</sub></td>
      <td align="center">58.67<br><sub>+2.00 pp</sub></td>
      <td align="center">71.33<br><sub>+2.66 pp</sub></td>
      <td align="center">77.50<br><sub>+10.00 pp</sub></td>
      <td align="center">80.00<br><sub>+3.33 pp</sub></td>
      <td align="center">65.00<br><sub>0.00 pp</sub></td>
      <td align="center">34.88<br><sub>-9.31 pp</sub></td>
      <td align="center">60.00<br><sub>-6.00 pp</sub></td>
    </tr>
    <tr>
      <td align="left">Mini-Agent</td>
      <td align="center">80.64<br><sub>+4.21 pp</sub></td>
      <td align="center">59.76<br><sub>+1.22 pp</sub></td>
      <td align="center">66.67<br><sub>0.00 pp</sub></td>
      <td align="center">77.38<br><sub>+11.90 pp</sub></td>
      <td align="center">65.33<br><sub>+6.00 pp</sub></td>
      <td align="center">80.00<br><sub>+5.67 pp</sub></td>
      <td align="center">75.00<br><sub>+9.17 pp</sub></td>
      <td align="center">76.67<br><sub>-3.33 pp</sub></td>
      <td align="center">72.50<br><sub>+2.50 pp</sub></td>
      <td align="center">48.84<br><sub>+20.93 pp</sub></td>
      <td align="center"><strong>58.00</strong><br><sub>+6.00 pp</sub></td>
    </tr>
  </tbody>
</table>


## 支持的 Harnesses

FlashRSI 当前支持七种 Harness 接入，其他接入正在规划中。

| Harness | 可编辑资源 | 接入说明 | 状态 |
| --- | --- | --- | --- |
| [MiniAgent](docs/usage.zh-CN.md#原生-harness) | 指令、skills、工具、运行代码与配置 | 原生 Python 入口，直接映射文件并使用资源角色标签。 | 可用 |
| [Claude Code](docs/agent-integration.md) | 指令、skills、子 Agent、hooks、MCP/LSP 与 settings | 映射产品公开的稳定接口，专有内部实现仍由原生 Harness 负责。 | 可用 |
| [Codex](docs/codex.md) | 指令、skills、子 Agent、hooks、插件、记忆、规则与配置 | 基于固定 Rust 基线的 Python 语义移植，在隔离进程中从 Snapshot 执行选定资源。 | 可用 |
| [Pi](docs/agent-integration.md) | Snapshot 内的纯 Python runtime、指令、skills、prompt templates 与 settings | 通过 `runtime/pi_native` 执行 Pi v1.4.0 的 headless loop 和 Pi 形状事件；不依赖 Node/npm。 | 可用 |
| [Hermes](docs/agent-integration.md) | Python Snapshot runtime、SOUL 指令、skills、工具配置 | 基于 v0.22.5 原上游 Python helper 与本地工具实现 headless 路径；不依赖宿主 Hermes CLI。 | 可用 |
| [OpenClaw](docs/agent-integration.md) | 纯 Python Snapshot runtime、bootstrap 文件、skills、工具、策略、会话与记忆配置 | Snapshot 内的 `runtime/openclaw_native` 遵循固定版本的嵌入式执行路径，支持 Chat Completions、Responses 和 Anthropic Messages；不依赖 Node/npm/Gateway。 | 可用 |
| [OpenCode](docs/agent-integration.md) | 纯 Python Snapshot runtime、指令、工具、权限、会话与配置 | Snapshot 内的 `runtime/opencode_native` 遵循固定 v1.19.3 的嵌入式执行路径，支持 Chat Completions、Responses 和 Anthropic Messages；不依赖 Bun/Node/CLI。 | 可用 |

## 支持的 Benchmarks

FlashRSI 支持 **30 个 benchmark 套件**，覆盖问答、推理、数学、代码、网页研究、文档、工具使用和交互任务。下表列出各套件的任务类型和主指标。**29 个套件提供 34 份固定的 Hugging Face 配置**，同时保留原生数据源。具体数据、服务与环境要求见[配置索引](docs/benchmark-quickstart.md)。

| 领域 | Benchmark | 任务 | 主指标 |
| --- | --- | --- | --- |
| 问答 | [SearchQA](flashrsi/benchmarks/searchqa) | 提供搜索片段的短答案问答 | Exact Match |
| 问答 | [HotpotQA](flashrsi/benchmarks/hotpotqa) | Wikipedia 多跳问答 | Joint F1 |
| 问答 | [MuSiQue](flashrsi/benchmarks/musique) | 组合式多跳问答 | Answer F1 |
| 问答 | [TriviaQA](flashrsi/benchmarks/triviaqa) | 基于 Wikipedia 上下文的知识问答 | Exact Match |
| 问答 | [Natural Questions](flashrsi/benchmarks/nq) | Wikipedia 问答，短答案与 yes/no 子集 | Exact Match |
| 问答 | [2WikiMultiHopQA](flashrsi/benchmarks/twowiki_multihopqa) | 基于给定上下文的多跳问答 | Exact Match |
| 推理 | [BBEH](flashrsi/benchmarks/bbeh) | BIG-Bench Extra Hard 复杂推理 | Accuracy |
| 推理 | [SuperGPQA](flashrsi/benchmarks/supergpqa) | 研究生级别多项选择题 | Accuracy |
| 推理 | [HLE](flashrsi/benchmarks/hle) | Humanity's Last Exam，仅文本子集 | Accuracy |
| 数学 | [MATH](flashrsi/benchmarks/math) | 竞赛数学 | Accuracy |
| 数学 | [AIME](flashrsi/benchmarks/aime) | AIME 2025 / 2026 数学题 | Accuracy |
| 数学 | [LiveMathBench](flashrsi/benchmarks/livemathbench) | 多次采样的数学评测 | mG-Pass@k |
| 代码 | [SWE-bench Verified](flashrsi/benchmarks/swe_bench) | 代码仓库 issue 修复 | Resolved rate |
| 代码 | [SWE-bench Pro V2](flashrsi/benchmarks/swe_bench_pro) | 代码仓库修复，all / hard 题集 | Resolved rate |
| 代码 | [DeepSWE](flashrsi/benchmarks/deepswe) | 仓库修复，通过 Harbor 运行 DeepSWE v1.1 任务 | Reward |
| 代码 | [Terminal-Bench 2.0 / 2.1](flashrsi/benchmarks/terminal_bench) | 通过 Harbor 执行的 Linux 终端任务 | Reward |
| 网页研究 | [BrowseComp](flashrsi/benchmarks/browsecomp) | 网络信息搜索 | Accuracy |
| 网页研究 | [DeepSearchQA](flashrsi/benchmarks/deepsearchqa) | 单答案与集合答案的网络搜索 | F1 |
| 网页研究 | [WideSearch](flashrsi/benchmarks/widesearch) | 表格型广泛信息搜索 | Score |
| 文档 | [OfficeQA-Pro](flashrsi/benchmarks/officeqa_pro) | 文档检索问答 | Accuracy |
| 文档 | [Harvey LAB](flashrsi/benchmarks/harvey_lab) | 法律文档任务 | All-pass rate |
| 文档 | [JobBench](flashrsi/benchmarks/jobbench) | 专业文件交付 | Reward |
| 文档 | [SpreadsheetBench 2](flashrsi/benchmarks/spreadsheetbench2) | Debugging / Financial_Model / Template | Reward |
| 工具使用 | [AppWorld](flashrsi/benchmarks/appworld) | 应用与 API 自动化 | Reward |
| 工具使用 | [BFCL V4](flashrsi/benchmarks/bfcl) | 多轮函数调用，仅 Base track | Reward |
| 工具使用 | [WorkBench](flashrsi/benchmarks/workbench) | 工作场景工具操作 | Correctness |
| 工具使用 | [TravelPlanner](flashrsi/benchmarks/travelplanner) | 旅行行程规划，仅 sole-planning track | Final pass rate |
| 交互环境 | [τ²-bench](flashrsi/benchmarks/tau2) | 航空、零售、电信多轮对话，文本模式 | Reward |
| 交互环境 | [τ³ Banking](flashrsi/benchmarks/tau3_banking) | 多轮银行客服 | Reward |
| 交互环境 | [ALFWorld](flashrsi/benchmarks/alfworld) | 纯文本交互环境 | Success rate |

<details>
<summary><strong>常见问题</strong></summary>

### 如何添加新的 benchmark？

实现一个产出 `Task` 并评估 `Trajectory` 的 `Benchmark`（或 `NativeBenchmark`），在 `flashrsi/benchmarks/__init__.py` 中注册，添加配置示例，并记录数据来源、版本、划分策略、指标和环境要求。可参考[benchmark 配置索引](docs/benchmark-quickstart.md)和[benchmark 接口](flashrsi/core/benchmark.py)。

### 如何添加新的算法？

继承 `Algorithm`，声明可观察和可编辑的资源范围，并将提议、筛选和记忆逻辑放在 `evolve` 中。使用共享的 Snapshot checkout、rollout 和评分操作，不要把算法策略移入 runner。可从现有[算法实现](flashrsi/algorithms)和[使用指南](docs/usage.zh-CN.md#扩展-flashrsi)开始。

### 如何添加新的 Harness？

在 `components.yaml` 中声明可编辑资源，实现调用原生入口的 `Agent` adapter，并返回 `Trajectory`。对于闭源产品映射接口文件，对于大型开源系统实现最小可信运行时，对于干净的开源 Harness 直接映射带角色标签的文件。详见 [Agent integration](docs/agent-integration.md)。

### 什么是 Snapshot？

Snapshot 是实验中选定 Harness 资源的不可变、可执行版本，包含定义行为的文件以及父子关系和变更记录；凭据、任务数据、日志和其他运行时状态保存在 Snapshot 之外。Snapshot 通常保存为 `runs/<run-id>/snapshots/<snapshot-id>.json`，其文件内容使用规范化 Base64 编码，ID 是文件内容和版本元数据的 SHA-256 哈希。

</details>

<details id="citation">
<summary>引用</summary>

```bibtex
@misc{jin2026flashrsi,
  title  = {FlashRSI: A Unified Framework for Agent Self-Improvement},
  author = {Jiajie Jin and Yuze Li and Yuyang Hu and Guanting Dong and
            Xiaoxi Li and Xinyu Yang and Zhicheng Dou and Yutao Zhu},
  year   = {2026}
}
```

</details>

---

<p align="center">
  <b>中国人民大学 · 高瓴人工智能学院</b>
</p>
