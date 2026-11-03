# Installation

Use Python **3.12** for a new environment. The framework supports Python 3.11+, but some integrations have narrower requirements. You only need Docker for benchmarks that run containerized tasks.

## Install the framework

```bash
git clone https://github.com/RUC-NLPIR/FlashRSI.git
cd FlashRSI
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[all]"
flashrsi --help
```

On Windows, activate with `.venv\Scripts\Activate.ps1` in PowerShell. Use Linux or WSL for integrations that require Linux containers or POSIX tools.

The `all` extra installs the Python dependencies for the built-in agents and
all non-Harbor benchmark integrations. It does not install external task
services, system tools, or Harbor itself.

## Add Harbor when needed

Harbor is the only separate extra. Add it to the same environment when using a
Harbor-backed benchmark:

```bash
python -m pip install -e ".[all,harbor]"
```

The pinned τ²-bench package is installed from its upstream repository by the
[experiment launcher](../experiment-launcher.zh-CN.md). Its LiteLLM requirement
conflicts with Harbor, so do not install τ²-bench into an environment that also
contains the `harbor` extra. The launcher creates or reuses the appropriate
environment automatically:

```bash
python scripts/run_experiment.py --config examples/quickstart_launch.yaml --plan
python scripts/run_experiment.py --config examples/quickstart_launch.yaml --check-only
```

`--plan` reports requirements; `--check-only` prepares dependencies and data without model calls. Some datasets require separate access or credentials; check the linked integration guide.

## Install Docker when needed

Install [Docker Desktop](https://docs.docker.com/desktop/) or [Docker Engine](https://docs.docker.com/engine/install/), start its Linux daemon, and verify:

```bash
docker version
docker compose version
docker run --rm hello-world
```

SearchQA and HotpotQA in `context` mode require Docker to run each Target task in a separate Linux container. The container must reach the configured model endpoint. Harbor tasks require Docker and Compose. SWE-bench Verified, AppWorld, and BFCL have their own container integrations. The [benchmark index](../integrations/benchmarks.md) identifies which environment to prepare.

## Check a first configuration

```bash
flashrsi run --config examples/quickstart.yaml --check-only
```

This local SearchQA configuration requires a running Docker Linux daemon, including for `--check-only`. Continue with the [quickstart](../index.md#run-the-experiment) to supply model settings and run evolution.
