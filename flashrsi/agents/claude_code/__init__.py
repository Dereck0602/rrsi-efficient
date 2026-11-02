"""Load versioned Claude Code extensions and retain the native event stream."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import aclosing
from dataclasses import fields, is_dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from flashrsi._records import _redact, _secrets
from flashrsi.core import Agent, Trajectory

_LOGGER = logging.getLogger(__name__)
_PROVIDER_ENV = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_GATEWAY",
)


class ClaudeCode(Agent):
    """Use a fresh SDK session for each prompt or file-editing task."""

    name = "claude_code"
    description = "Claude Code with versioned native extensions and delegated tasks."

    _container_dependencies = ("claude-agent-sdk",)
    # Appended to the native claude_code preset. Absent from the seed, so an
    # algorithm that targets it creates the file.
    instruction_file = "CLAUDE.md"

    def _check_container(self) -> None:
        super()._check_container()
        if self.config.get("cli_path"):
            raise ValueError("Container execution installs its own CLI; omit cli_path")
        if self.config["llm"].get("auth", "api_key") == "login":
            raise ValueError(
                "Claude Code containers cannot use llm.auth: login; "
                "configure llm.api_key, llm.auth_token, or llm.oauth_token"
            )
        _authentication_env(self.config["llm"])

    async def _run(self, prompt: str, working_dir: Path | None = None) -> Trajectory:
        # Import only when used: other Agents do not require this optional SDK.
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ClaudeSDKClient,
            ResultMessage,
            SystemMessage,
        )

        config = self.config
        llm = config["llm"]
        try:
            env = _authentication_env(llm)
        except ValueError as exc:
            return Trajectory(status="error", error_message=str(exc))
        env.update(
            {
                "CLAUDE_PLUGIN_ROOT": str(self.files_dir),
                "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
                "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "",
            }
        )
        secrets = sorted(
            set(_secrets(os.environ) + _secrets(env) + _secrets(config)),
            key=len,
            reverse=True,
        )
        timeout = config.get("timeout", 300)
        messages = [
            {"type": "RunInput", "prompt": prompt, "snapshot_id": self.snapshot.id}
        ]
        result = None
        usage = {}
        error = None
        stderr_tail = ""

        def stderr(line):
            nonlocal stderr_tail
            line = _redact(line, secrets)
            _LOGGER.debug("%s", line)
            stderr_tail = (stderr_tail + line + "\n")[-8192:]

        try:
            try:
                instructions = (self.files_dir / "CLAUDE.md").read_text(
                    encoding="utf-8"
                )
            except FileNotFoundError:
                instructions = ""
            if config.get("_worker_system_prompt"):
                instructions += "\n\n" + config["_worker_system_prompt"]
            settings = _settings(self.files_dir / "settings.json")
        except (OSError, UnicodeError, ValueError) as exc:
            return Trajectory(
                status="error",
                messages=_redact(messages, secrets),
                error_message=_redact(str(exc), secrets),
            )

        with TemporaryDirectory(prefix="flashrsi-claude-code-") as temporary:
            runtime = Path(temporary).resolve()
            env["CLAUDE_CONFIG_DIR"] = str(runtime / "state")
            # Only explicit login mode may consult the user's native credential
            # location. API credentials and all transcripts stay run-local.
            env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = (
                os.environ.get(
                    "CLAUDE_SECURESTORAGE_CONFIG_DIR",
                    os.environ.get("CLAUDE_CONFIG_DIR", ""),
                )
                if llm.get("auth", "api_key") == "login"
                else str(runtime / "state")
            )
            env["CLAUDE_CODE_TMPDIR"] = str(runtime / "tmp")
            (runtime / "tmp").mkdir(mode=0o700)
            task_dir = working_dir or runtime / "work"
            task_dir.mkdir(exist_ok=True)
            # cwd is the task/edit target. Configuration always comes from our
            # version, even when the target has its own CLAUDE.md or .claude/.
            system_prompt = {"type": "preset", "preset": "claude_code"}
            if instructions.strip():
                system_prompt["append"] = instructions
            options = ClaudeAgentOptions(
                model=llm["model_name"],
                system_prompt=system_prompt,
                plugins=[{"type": "local", "path": str(self.files_dir)}],
                skills="all",
                setting_sources=[],
                strict_mcp_config=True,
                mcp_servers=str(self.files_dir / ".mcp.json")
                if (self.files_dir / ".mcp.json").is_file()
                else {},
                settings=settings,
                forward_subagent_text=True,
                include_hook_events=True,
                cwd=task_dir,
                env=env,
                permission_mode=config.get("permission_mode", "acceptEdits"),
                allowed_tools=config.get("allowed_tools", ["Read", "Glob", "Grep"]),
                disallowed_tools=config.get("disallowed_tools", []),
                tools=config.get("tools"),
                max_turns=config.get("max_turns", 10),
                max_budget_usd=config.get("max_budget_usd"),
                # Tool output and long reasoning can exceed the SDK's 1 MiB default.
                max_buffer_size=config.get("max_buffer_size", 32 * 1024 * 1024),
                effort=llm.get("effort"),
                cli_path=Path(config["cli_path"]).expanduser().resolve()
                if config.get("cli_path")
                else None,
                stderr=stderr,
            )
            messages[0]["system_prompt"] = options.system_prompt
            client = ClaudeSDKClient(options=options)
            try:
                try:
                    async with asyncio.timeout(timeout):
                        await client.connect()
                        await client.query(prompt)
                        async with aclosing(
                            _messages(
                                client,
                                completion_grace=config.get("completion_grace", 0.5),
                            )
                        ) as stream:
                            async for message in stream:
                                messages.append(_record(message))
                                if isinstance(message, SystemMessage):
                                    error = _extension_error(message)
                                    if error:
                                        break
                                if isinstance(message, ResultMessage):
                                    result = message
                                    _add_usage(usage, result)
                                    error = _result_error(result)
                                    if error:
                                        break
                finally:
                    # Cleanup has its own bounded SDK waits. The execution
                    # deadline or caller cancellation must not interrupt them.
                    await _disconnect(client)
            except TimeoutError:
                error = f"Claude Code timed out after {timeout:g}s"
            except Exception as exc:
                # SDK control failures also use plain Exception. Cancellation
                # is a BaseException and still propagates after cleanup.
                error = str(exc) or type(exc).__name__

        if result is None:
            error = error or "Claude Code ended without a result"
        if error and stderr_tail:
            error += "\nClaude Code stderr (tail):\n" + stderr_tail.rstrip()

        return Trajectory(
            status="error" if error else "finished",
            output=_redact(result.result or "", secrets) if not error else "",
            messages=_redact(messages, secrets),
            usage=_redact(usage, secrets),
            error_message=_redact(error, secrets),
        )


def _authentication_env(llm):
    """Map explicit connection settings to the native CLI's environment."""
    auth = llm.get("auth", "api_key")
    if auth not in ("api_key", "login"):
        raise ValueError("Claude Code llm.auth must be 'api_key' or 'login'")
    credentials = {
        "api_key": "ANTHROPIC_API_KEY",
        "auth_token": "ANTHROPIC_AUTH_TOKEN",
        "oauth_token": "CLAUDE_CODE_OAUTH_TOKEN",
    }
    provided = [name for name in credentials if llm.get(name)]
    if auth == "login" and provided:
        raise ValueError("Claude Code llm.auth: login cannot include API credentials")
    if auth == "api_key" and len(provided) != 1:
        raise ValueError(
            "Claude Code requires exactly one of llm.api_key, llm.auth_token, "
            "or llm.oauth_token; use llm.auth: login for native login"
        )
    # Empty values override inherited credentials; the endpoint has an explicit
    # default so a parent ANTHROPIC_BASE_URL cannot redirect this role.
    env = {name: "" for name in credentials.values()}
    # This adapter supports the configured Anthropic-compatible endpoint and
    # explicit native login, not cloud-provider routing inherited from the CLI.
    env.update({name: "" for name in _PROVIDER_ENV})
    env["ANTHROPIC_BASE_URL"] = llm.get("base_url") or "https://api.anthropic.com"
    for field, variable in (
        ("max_tokens", "CLAUDE_CODE_MAX_OUTPUT_TOKENS"),
        ("max_retries", "CLAUDE_CODE_MAX_RETRIES"),
    ):
        if field in llm:
            value = llm[field]
            minimum = 1 if field == "max_tokens" else 0
            if type(value) is not int or value < minimum:
                raise ValueError(
                    f"Claude Code llm.{field} must be an integer >= {minimum}"
                )
            env[variable] = str(value)
    for name in provided:
        if not isinstance(llm[name], str) or not llm[name].strip():
            raise ValueError(f"Claude Code llm.{name} must be a non-empty string")
        env[credentials[name]] = llm[name]
    return env


def _settings(path):
    """Validate settings the CLI can otherwise silently ignore.

    Runtime authentication and storage cannot be replaced by a candidate's
    settings.env. Other native settings are passed through unchanged.
    """
    if not path.is_file():
        return None
    try:
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
    except ValueError as exc:
        raise ValueError(f"Invalid {path.name}: {exc}") from None
    if not isinstance(settings, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    environment = settings.get("env", {})
    if not isinstance(environment, dict) or any(
        not isinstance(value, str) for value in environment.values()
    ):
        raise ValueError(f"{path.name}.env must map names to strings")
    reserved = {
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_SECURESTORAGE_CONFIG_DIR",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY",
        "CLAUDE_CODE_TMPDIR",
        *_PROVIDER_ENV,
    }
    for name in environment:
        if name.upper() in reserved or name.upper().endswith(
            ("API_KEY", "TOKEN", "SECRET", "PASSWORD")
        ):
            raise ValueError(
                f"{path.name}.env cannot set runtime field {name}; "
                "use Agent runtime config"
            )
    return str(path)


def _extension_error(message):
    if message.subtype != "init":
        return None
    failures = [
        json.dumps(item, ensure_ascii=False)
        for item in message.data.get("plugin_errors", ())
    ]
    failures.extend(
        f"MCP server {server['name']}: {server['status']}"
        for server in message.data.get("mcp_servers", ())
        if server["status"] in {"failed", "needs-auth"}
    )
    if failures:
        return "Claude Code extensions failed to load: " + "; ".join(failures)
    return None


def _result_error(result):
    if (
        result.deferred_tool_use is not None
        or result.terminal_reason == "tool_deferred"
    ):
        return "Claude Code deferred a tool; this run cannot resume deferred work"
    if result.terminal_reason in {"aborted_streaming", "aborted_tools"}:
        return f"Claude Code ended with {result.terminal_reason}"
    if result.is_error or result.subtype != "success":
        return (
            "; ".join(result.errors or [])
            or result.result
            or f"Claude Code ended with {result.subtype}"
        )
    return None


async def _messages(client, *, completion_grace):
    """Collect delegated work and its parent continuation before closing the SDK.

    The CLI has turn-end events but no run-end event. Once known agent tasks and
    notification turns finish, drain trailing events for a short quiet interval.
    Background shells are not finite delegated work and do not hold the run open.
    """
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeSDKError,
        ResultMessage,
        SystemMessage,
        TaskNotificationMessage,
        TaskStartedMessage,
        TaskUpdatedMessage,
    )

    active = set()
    delegated = set()
    background = set()
    notifications = set()
    handling = set()
    in_turn = True
    result = None
    async with aclosing(client.receive_messages()) as stream:
        while True:
            settled = result is not None and not (active or notifications or in_turn)
            if settled and not delegated:
                return
            try:
                if settled:
                    try:
                        async with asyncio.timeout(completion_grace):
                            message = await anext(stream)
                    except TimeoutError:
                        return
                else:
                    message = await anext(stream)
            except StopAsyncIteration:
                if not settled:
                    raise ClaudeSDKError(
                        "Claude Code stream ended before delegated work and its "
                        "parent response completed"
                    ) from None
                return

            if isinstance(message, TaskStartedMessage):
                if message.task_type in {"local_agent", "local_workflow"}:
                    delegated.add(message.task_id)
                    active.add(message.task_id)
                    if message.data.get("is_backgrounded") is not False:
                        background.add(message.task_id)
            elif isinstance(message, TaskUpdatedMessage):
                if message.status in {"completed", "failed", "stopped", "killed"}:
                    active.discard(message.task_id)
            elif isinstance(message, TaskNotificationMessage):
                active.discard(message.task_id)
                if message.task_id in background and message.status in {
                    "completed",
                    "failed",
                }:
                    notifications.add(message.task_id)
            elif (
                isinstance(message, SystemMessage)
                and message.subtype == "background_tasks_changed"
            ):
                # Foreground agents may be backgrounded without another start
                # event. This snapshot marks that transition, not task completion.
                background.update(
                    task["task_id"]
                    for task in message.data.get("tasks", ())
                    if task["task_id"] in delegated
                )
            elif isinstance(message, SystemMessage) and message.subtype == "init":
                # Each auto-generated notification turn has its own init. New
                # notifications arriving during this turn belong to a later one.
                in_turn = True
                handling = notifications.copy()
            elif (
                isinstance(message, AssistantMessage)
                and message.parent_tool_use_id is None
                and not in_turn
            ):
                in_turn = True
                handling = notifications.copy()
            elif isinstance(message, ResultMessage):
                result = message
                in_turn = False
                notifications.difference_update(handling)
                handling.clear()
                if message.is_error or message.subtype != "success":
                    yield message
                    return
            yield message


def _add_usage(usage, result):
    """Sum turn counters; native model totals and cost are session cumulative."""
    _sum_counters(usage, result.usage or {})
    usage["num_turns"] = usage.get("num_turns", 0) + result.num_turns
    usage["duration_ms"] = usage.get("duration_ms", 0) + result.duration_ms
    usage.update(
        model_usage=result.model_usage,
        total_cost_usd=result.total_cost_usd,
        duration_api_ms=result.duration_api_ms,
        session_id=result.session_id,
    )


def _sum_counters(total, values):
    for key, value in values.items():
        if isinstance(value, dict):
            _sum_counters(total.setdefault(key, {}), value)
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            total[key] = total.get(key, 0) + value
        else:
            total[key] = value


async def _disconnect(client):
    cleanup = asyncio.create_task(client.disconnect())
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


def _record(value):
    """Keep SDK message and content-block types along with their native fields."""
    if is_dataclass(value):
        return {
            "type": type(value).__name__,
            **{
                field.name: _record(getattr(value, field.name))
                for field in fields(value)
            },
        }
    if isinstance(value, dict):
        return {key: _record(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_record(item) for item in value]
    return value
