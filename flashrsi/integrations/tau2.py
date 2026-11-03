"""Run an existing FlashRSI Agent inside one native tau2 simulation.

tau2 owns the conversation loop, the simulated customer, the tool
implementations, the database and the reward. This integration only bridges
two directions: a FlashRSI Agent is presented to tau2 as one of its agents,
and the resulting SimulationRun is translated back into a Trajectory.

The Agent speaks through text, so tau2's tool schemas are rendered into the
prompt and a ``TOOL: {...}`` line in the reply becomes a tau2 tool call. This
keeps every harness usable without a native tool-calling interface.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from pathlib import Path

try:
    from tau2.agent.base_agent import HalfDuplexAgent
    from tau2.data_model.message import AssistantMessage, ToolCall
    from tau2.registry import registry
    from tau2.run import TextRunConfig, run_single_task
except ImportError as exc:
    raise ImportError("Install pinned tau2-bench alongside flashrsi-lite[all]") from exc

from flashrsi._records import _config_record, _redact, _secrets, _trajectory_record
from flashrsi.core import Trajectory
from flashrsi.integrations import _model_egress, supplied_context
from flashrsi.integrations._tau2_evidence import _Evidence
from flashrsi.integrations._tau2_turns import run_bounded

_AGENT_NAME = "flashrsi-lite"
_DOMAIN = "banking_knowledge"
# Native modules cache global paths and Judge defaults at import time. Scope
# rebinding and restoration to one simulation, including concurrent callers.
_SIMULATION_LOCK = threading.RLock()
# tau2 builds the agent through a registered factory, which receives no
# reference to the caller; this carries the running Agent into it.
_CURRENT: ContextVar[dict] = ContextVar("tau2_agent")
_PREFIX = "TOOL:"
_PROTOCOL = (
    f"Reply with one line starting with {_PREFIX} followed by JSON "
    '{"name": ..., "arguments": {...}} to call a tool. Any other reply is '
    "spoken to the customer."
)


def bind(
    agent, user_simulator: Mapping, *, task_timeout: int | None = None, judge=None
) -> None:
    """Connect an Agent to tau2; CLI runs do this in the runner.

    The simulated customer is played by its own model, configured under
    ``benchmark.user_simulator.llm``. It must never share the evolution
    worker's configuration: the worker edits the Agent being measured.
    """
    llm = dict((user_simulator or {}).get("llm") or {})
    model = llm.get("model_name")
    if not isinstance(model, str) or not model.strip():
        raise ValueError(
            "tau needs its own user simulator model; set "
            "benchmark.user_simulator.llm.model_name"
        )
    if task_timeout is not None and (type(task_timeout) is not int or task_timeout < 1):
        raise ValueError("tau task_timeout must be a positive integer in seconds")
    supplied_context._check(agent)
    _model_egress._origins(agent.config)
    judge_llm = dict((judge or {}).get("llm") or {})
    if judge_llm and not judge_llm.get("model_name"):
        raise ValueError(
            "benchmark.judge.llm.model_name is required when configuring Judge"
        )

    async def run(target, task):
        return await _run(target, task, llm, task_timeout, judge_llm)

    agent._env_runner = run


async def _run(
    agent, task, simulator: Mapping, task_timeout: int | None = None, judge=None
) -> Trajectory:
    """The caller's rollout has already pinned its Snapshot and configuration."""
    config = copy.deepcopy(agent.config)
    secrets = sorted(
        set(
            _secrets(config)
            + _secrets(simulator)
            + _secrets(judge or {})
            + _secrets(os.environ)
        ),
        key=len,
        reverse=True,
    )
    record = {
        "type": "RunConfig",
        "config": _config_record(config),
        "snapshot_id": agent.snapshot.id,
    }
    event = {
        "type": "Tau2Simulator",
        "task_id": task.task_id,
        "task_checksum": task.metadata["task_checksum"],
        "domain": task.metadata.get("domain", _DOMAIN),
        "user_simulator": _config_record(simulator),
        "judge": _config_record(judge or {}),
    }
    trajectory = Trajectory(messages=(record,))
    evidence = _Evidence(secrets=secrets)
    try:
        domain = Path(task.env)
        if not domain.is_dir():
            raise ValueError(f"tau task has no domain directory: {domain}")
        if task_timeout is None:
            loop = asyncio.get_running_loop()
            active_turn = None
            stopped = False

            async def solve_turn(prompt):
                nonlocal active_turn
                if stopped:
                    raise RuntimeError("Tau simulation has stopped")
                active_turn = asyncio.current_task()
                try:
                    return await supplied_context._run_prompt(
                        agent, prompt, model_only=True
                    )
                finally:
                    active_turn = None

            def run_turn(prompt):
                return asyncio.run_coroutine_threadsafe(
                    solve_turn(prompt), loop
                ).result()

            try:
                run = await asyncio.to_thread(
                    _simulate,
                    run_turn,
                    task,
                    domain,
                    simulator,
                    config,
                    evidence,
                    judge,
                )
            finally:
                stopped = True
                if active_turn is not None:
                    active_turn.cancel()
                    await asyncio.gather(active_turn, return_exceptions=True)
            result = _simulation_result(run, evidence)
        else:
            result = await _isolated_simulation(
                agent, task, simulator, config, task_timeout, judge
            )
        trajectory = Trajectory(
            messages=(record, *result["messages"]), usage=result["usage"]
        )
        if result.get("error"):
            raise RuntimeError(result["error"])
        reward = result["reward"]
        if reward is None:
            raise ValueError("tau2 simulation ended without a reward")
        event["reward"] = reward
        event["termination_reason"] = result["termination_reason"]
        if result.get("reward_info") is not None:
            event["reward_info"] = result["reward_info"]
        if result.get("hard_wallclock_limit_seconds"):
            event["hard_wallclock_limit_seconds"] = task_timeout
            event["usage_incomplete"] = True
        if result.get("evidence_truncated"):
            event["evidence_truncated"] = True
        trajectory = Trajectory(
            status="finished",
            output=result["output"],
            messages=(record, *result["messages"]),
            usage=result["usage"],
        )
    except Exception as exc:
        error = str(exc) or type(exc).__name__
        event["error"] = error
        if task_timeout is None:
            partial = evidence.read()
            trajectory = replace(
                trajectory,
                messages=(record, *partial["messages"]),
                usage=partial["usage"],
            )
        trajectory = replace(trajectory, status="error", error_message=error)
    return replace(
        trajectory,
        output=_redact(trajectory.output, secrets),
        messages=_redact((*trajectory.messages, event), secrets),
        usage=_redact(trajectory.usage, secrets),
        error_message=_redact(trajectory.error_message, secrets),
    )


async def _isolated_simulation(
    agent, task, simulator, config, task_timeout, judge=None
):
    """A hard deadline needs an OS process: cancelling to_thread leaves tau2 running."""
    if os.name != "posix":
        raise RuntimeError("tau hard task_timeout currently requires a POSIX host")
    with tempfile.TemporaryDirectory(prefix="flashrsi-tau2-") as directory:
        root = Path(directory)
        request = root / "request.json"
        response = root / "response.json"
        evidence = _Evidence(root / "evidence.jsonl")
        request.write_text(
            json.dumps(
                {
                    "config": {"max_steps": config.get("max_steps", 30)},
                    "task_id": task.task_id,
                    "domain": task.env,
                    "metadata": dict(task.metadata),
                    "simulator": dict(simulator),
                    "judge": dict(judge or {}),
                    "evidence_path": str(evidence.path),
                },
                default=lambda value: (
                    str(value) if isinstance(value, Path) else dict(value)
                ),
            ),
            encoding="utf-8",
        )
        request.chmod(0o600)

        async def run_turn(prompt):
            return await supplied_context._run_prompt(agent, prompt, model_only=True)

        returncode, timed_out = await run_bounded(
            [
                sys.executable,
                "-m",
                "flashrsi.integrations._tau2_task",
                str(request),
                str(response),
            ],
            run_turn,
            task_timeout,
        )
        if timed_out:
            return {
                **evidence.read(),
                "reward": 0.0,
                "termination_reason": "timeout",
                "hard_wallclock_limit_seconds": task_timeout,
                "output": "",
            }
        if not response.is_file():
            return {
                **evidence.read(),
                "error": f"tau2 task process exited {returncode} without a result",
            }
        result = json.loads(response.read_text(encoding="utf-8"))
        return result


def _simulate(run_turn, task, domain, simulator, config, evidence=None, judge=None):
    """Run tau2's native loop with a callback for each isolated Target turn."""
    token = _CURRENT.set({"run_turn": run_turn, "evidence": evidence})
    try:
        domain_name = task.metadata.get("domain", _DOMAIN)
        with (
            _SIMULATION_LOCK,
            _domain_paths(
                domain, domain_name, task.metadata.get("prepared_user_simulator")
            ),
            _judge_settings(judge),
        ):
            _register_once()
            return run_single_task(
                TextRunConfig(
                    domain=domain_name,
                    agent=_AGENT_NAME,
                    user="user_simulator",
                    llm_user=_llm_model(simulator),
                    llm_args_user=_llm_args(simulator),
                    max_steps=config.get("max_steps", 30),
                    retrieval_config=task.metadata.get("variant"),
                ),
                _native_task(domain, task),
                seed=task.metadata.get("simulation_seed"),
            )
    finally:
        _CURRENT.reset(token)


@contextmanager
def _domain_paths(domain: Path, domain_name=_DOMAIN, user_simulator=None):
    """Point tau2 at the verified domain this Task came from.

    tau2 resolves TAU2_DATA_DIR once at import, and the domain modules copy
    those paths into their own constants, so the environment variable cannot
    redirect an already-imported package. Rebind every path that names the
    installed domain, including the template baked into each variant.
    """
    if domain_name not in ("banking_knowledge", "airline", "retail", "telecom"):
        raise ValueError(f"Unsupported tau domain: {domain_name}")
    utils = importlib.import_module(f"tau2.domains.{domain_name}.utils")
    environment = importlib.import_module(f"tau2.domains.{domain_name}.environment")
    directory_name = (
        "KNOWLEDGE_DATA_DIR"
        if domain_name == _DOMAIN
        else f"{domain_name.upper()}_DATA_DIR"
    )
    installed = getattr(utils, directory_name)
    modules = [utils, environment]
    variants = []
    if domain_name == _DOMAIN:
        from tau2.domains.banking_knowledge import retrieval

        modules.append(retrieval)
        variants = [
            (variant, variant.prompt_template)
            for variant in retrieval.RETRIEVAL_VARIANTS.values()
            if variant.prompt_template.is_relative_to(installed)
        ]
    originals = [
        (module, name, value)
        for module in modules
        for name, value in vars(module).items()
        if isinstance(value, Path) and value.is_relative_to(installed)
    ]
    # The prepared package includes the same native prompts. Their module-level
    # paths also need rebinding when tau2 was imported before dataset resolution.
    user_paths = []
    if user_simulator is not None:
        user_module = importlib.import_module("tau2.user.user_simulator")
        installed_user = user_module.GLOBAL_USER_SIM_GUIDELINES_DIR
        user_paths = [
            (name, value)
            for name, value in vars(user_module).items()
            if isinstance(value, Path) and value.is_relative_to(installed_user)
        ]

    def rebind(path: Path) -> Path:
        return domain / path.relative_to(installed)

    try:
        for module, name, value in originals:
            setattr(module, name, rebind(value))
        for variant, template in variants:
            object.__setattr__(variant, "prompt_template", rebind(template))
        for name, value in user_paths:
            setattr(
                user_module,
                name,
                Path(user_simulator) / value.relative_to(installed_user),
            )
        yield
    finally:
        for module, name, value in originals:
            setattr(module, name, value)
        for variant, template in variants:
            object.__setattr__(variant, "prompt_template", template)
        for name, value in user_paths:
            setattr(user_module, name, value)


@contextmanager
def _judge_settings(judge):
    """Keep the native NL grader; only configure its independent model/connection."""
    if not judge:
        yield
        return
    from tau2.evaluator import evaluator_nl_assertions as evaluator

    model, args = (
        evaluator.DEFAULT_LLM_NL_ASSERTIONS,
        evaluator.DEFAULT_LLM_NL_ASSERTIONS_ARGS,
    )
    try:
        evaluator.DEFAULT_LLM_NL_ASSERTIONS = _llm_model(judge)
        evaluator.DEFAULT_LLM_NL_ASSERTIONS_ARGS = {**args, **_llm_args(judge)}
        yield
    finally:
        evaluator.DEFAULT_LLM_NL_ASSERTIONS = model
        evaluator.DEFAULT_LLM_NL_ASSERTIONS_ARGS = args


def _register_once() -> None:
    """tau2 keeps factories by name and has no API to replace one, so register
    a single factory that reads whichever Agent is running this task."""
    if _AGENT_NAME not in registry.get_agents():
        registry.register_agent_factory(
            lambda tools, domain_policy, **kwargs: _AgentBridge(
                tools, domain_policy, **_CURRENT.get()
            ),
            _AGENT_NAME,
        )


def _llm_args(simulator: Mapping) -> dict:
    """Pass connection details through under the names litellm expects."""
    return {
        "api_base" if key == "base_url" else key: value
        for key, value in simulator.items()
        if key != "model_name" and value is not None
    }


def _llm_model(simulator: Mapping) -> str:
    """litellm needs a provider prefix; a custom base_url speaks the OpenAI API."""
    model = simulator["model_name"]
    if "/" in model or not simulator.get("base_url"):
        return model
    return f"openai/{model}"


def _native_task(domain: Path, task):
    from tau2.data_model.tasks import Task as NativeTask

    path = domain / task.metadata["task_path"]
    payload = path.read_bytes()
    expected = task.metadata.get("task_file_checksum", task.metadata["task_checksum"])
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError("tau task data changed after benchmark.load()")
    row = json.loads(payload)
    if "native_task_id" in task.metadata:
        row = next(r for r in row if r["id"] == task.metadata["native_task_id"])
    return NativeTask.model_validate(row)


def _last_reply(run) -> str:
    for message in reversed(run.messages):
        if message.role == "assistant" and message.content:
            return message.content
    return ""


def _message(item) -> dict:
    return {
        "type": type(item).__name__,
        **item.model_dump(mode="json", exclude_none=True),
    }


def _simulation_result(run, evidence):
    partial = evidence.read()
    # Native dialogue is authoritative on completion; do not duplicate it with
    # the incremental copy. Internal harness executions remain diagnostic data.
    return {
        "reward": run.reward_info.reward if run.reward_info is not None else None,
        "reward_info": run.reward_info.model_dump(mode="json")
        if run.reward_info is not None
        else None,
        "termination_reason": str(run.termination_reason),
        "output": _last_reply(run),
        "messages": [
            *(_message(item) for item in run.messages),
            *(m for m in partial["messages"] if m["type"] != "Tau2Dialogue"),
        ],
        "usage": {
            **partial["usage"],
            "agent_cost": run.agent_cost,
            "user_cost": run.user_cost,
        },
    }


class _AgentBridge(HalfDuplexAgent):
    """Present a FlashRSI Agent to tau2 as one of its turn-based agents."""

    def __init__(self, tools, domain_policy, *, run_turn, evidence=None):
        super().__init__(tools, domain_policy)
        self._run_turn = run_turn
        self._evidence = evidence
        self._prompt = "\n\n".join(
            (
                domain_policy,
                "## Tools",
                *(tool.to_str() for tool in tools),
                _PROTOCOL,
            )
        )

    def get_init_state(self, message_history=None):
        if self._evidence is not None:
            for message in message_history or ():
                self._evidence.append(
                    {"type": "Tau2Dialogue", "message": _message(message)}
                )
        return list(message_history or ())

    def generate_next_message(self, message, state):
        """Keep native turn order while the Target solves in a container."""
        state = [*state, message]
        started = time.monotonic()
        if self._evidence is not None:
            self._evidence.append(
                {"type": "Tau2Dialogue", "message": _message(message)}
            )
            self._evidence.append({"type": "Tau2AgentStart", "started_at": time.time()})
        prompt = "\n\n".join(
            [self._prompt, "## Conversation", *(_turn(item) for item in state)]
        )
        trajectory = self._run_turn(prompt)
        if self._evidence is not None:
            self._evidence.append(
                {
                    "type": "Tau2AgentExecution",
                    "elapsed_seconds": time.monotonic() - started,
                    "trajectory": _trajectory_record(trajectory),
                }
            )
        if trajectory.status != "finished":
            raise RuntimeError(trajectory.error_message or "agent run failed")
        reply = _reply(trajectory.output)
        if self._evidence is not None:
            self._evidence.append({"type": "Tau2Dialogue", "message": _message(reply)})
        return reply, [*state, reply]


def _turn(message) -> str:
    if getattr(message, "tool_messages", None) is not None:
        return "\n\n".join(_turn(item) for item in message.tool_messages)
    content = getattr(message, "content", None) or ""
    calls = getattr(message, "tool_calls", None) or ()
    for call in calls:
        content = f"{content}\n{_PREFIX} {json.dumps({'name': call.name, 'arguments': call.arguments})}"
    return f"{message.role}: {content.strip()}"


def _reply(output: str) -> AssistantMessage:
    """Accept one explicit TOOL directive, including fenced/multiline JSON."""
    text = output.strip()
    directives = list(re.finditer(r"(?m)^[ \t]*TOOL:[ \t]*", text))
    if not directives:
        return AssistantMessage(role="assistant", content=output)
    try:
        if len(directives) != 1:
            raise ValueError("reply must contain exactly one tool directive")
        body = text[directives[0].end() :].lstrip()
        value, end = json.JSONDecoder().raw_decode(body)
        if body[end:].lstrip().startswith(("{", "[")):
            raise ValueError("reply contains more than one tool object")
        name, arguments = value["name"], value.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            raise ValueError("tool call needs a name and object arguments")
    except (ValueError, KeyError, TypeError):
        # Report the malformed attempt instead of silently speaking it.
        return AssistantMessage(
            role="assistant",
            content=f"[unparsable tool call, treated as speech] {output}",
        )
    return AssistantMessage(
        role="assistant",
        content=None,
        tool_calls=[
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:8]}",
                name=name,
                arguments=arguments,
                requestor="assistant",
            )
        ],
    )
