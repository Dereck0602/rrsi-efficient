"""Embedded OpenClaw agent loop, stripped of Gateway/channel concerns."""

from __future__ import annotations

import asyncio
import copy
import inspect
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import providers
from .compaction import compact
from .models import RuntimeEvent, message
from .policy import ToolPolicy
from .resources import load, snapshot_config
from .session import Session
from .tools import ToolRunner, schemas


def _tool_output(name: str, result: Mapping[str, Any], workspace: Path) -> str:
    """Render structured receipts as OpenClaw model-facing tool text."""

    if not result.get("ok", False):
        return f"Error: {result.get('error', 'tool execution failed')}"
    if isinstance(result.get("output"), str):
        return str(result["output"])
    if name == "read":
        path = Path(str(result.get("path", "")))
        if not path.is_absolute():
            path = workspace / path
        path = path.resolve()
        content = str(result.get("content", ""))
        start = int(result.get("line_start", 1) or 1)
        lines = content.splitlines()
        numbered = "\n".join(
            f"{start + index}: {line}" for index, line in enumerate(lines)
        )
        total = int(result.get("total_lines", len(lines)) or 0)
        end = int(result.get("line_end", start + len(lines) - 1) or start)
        footer = (
            f"(Showing lines {start}-{end} of {total}. Use offset={end + 1} to continue.)"
            if result.get("truncated")
            else f"(End of file - total {total} lines)"
        )
        return "\n".join(
            [
                f"<path>{path}</path>",
                "<type>file</type>",
                "<content>",
                numbered,
                footer,
                "</content>",
            ]
        )
    if name in {"exec", "process"}:
        return str(result.get("output", ""))
    if name == "write":
        return "Wrote file successfully."
    if name == "edit":
        return "Edit applied successfully."
    if name == "apply_patch":
        path = result.get("path")
        return "Success. Updated the following files:\nM " + str(path or "")
    if name in {"skill", "read_skill"}:
        skill_name = str(result.get("name", ""))
        content = str(result.get("content", ""))
        return f'<skill_content name="{skill_name}">\n{content}\n</skill_content>'
    if name == "memory_search":
        return str(result.get("output", "No memory matches"))
    if isinstance(result.get("content"), str):
        return str(result["content"])
    # Receipts are retained for events/session replay only. Never fall back to
    # serializing the whole structured result into the next model message.
    return str(result.get("message") or "Tool completed successfully.")


def _emit(events: list[dict[str, Any]], callback, event: RuntimeEvent) -> None:
    value = event.as_dict()
    events.append(value)
    if callback is not None:
        result = callback(copy.deepcopy(value))
        if inspect.isawaitable(result):
            # Event callbacks are normally async; the loop awaits them through
            # ``_notify`` when called from the async path.
            return result


async def _notify(events, callback, event):
    value = event.as_dict()
    events.append(value)
    if callback is not None:
        result = callback(copy.deepcopy(value))
        if inspect.isawaitable(result):
            await result


async def run(
    config: Mapping[str, Any],
    prompt: str,
    task_dir: Path,
    snapshot_root: Path,
    *,
    cancel_event: asyncio.Event | None = None,
    on_event=None,
    _state=None,
) -> dict[str, Any]:
    """Run one OpenClaw turn and return FlashRSI-compatible result fields."""
    config = dict(config)
    task_dir = Path(task_dir)
    snapshot_root = Path(snapshot_root)
    task_dir.mkdir(parents=True, exist_ok=True)
    events: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    state = _state if isinstance(_state, dict) else {}
    cancel_event = cancel_event or asyncio.Event()
    # The selected Snapshot owns defaults such as tool policy, prompt file
    # names and transcript format.  Explicit role configuration wins over
    # those defaults; credentials are never taken from the Snapshot config.
    snapshot_defaults = snapshot_config(snapshot_root)
    for key in ("prompt", "tools", "session", "memory", "compaction"):
        if key not in config and key in snapshot_defaults:
            config[key] = snapshot_defaults[key]
    workspace = Path(config.get("workspace") or task_dir).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    resources = load(snapshot_root, workspace, config)
    policy = ToolPolicy(config, workspace)
    tools = ToolRunner(workspace, snapshot_root, policy)
    session_cfg = config.get("session")
    session_cfg = session_cfg if isinstance(session_cfg, Mapping) else {}
    persist_session = bool(
        session_cfg.get("persist", config.get("session_persist", True))
    )
    configured_session_path = config.get("session_file") or session_cfg.get(
        "transcript"
    )
    if configured_session_path:
        session_path = Path(configured_session_path).expanduser()
        if not session_path.is_absolute():
            session_path = task_dir / session_path
    else:
        session_path = task_dir / ".openclaw" / "session.sqlite"
    session = Session(Path(session_path).expanduser()) if persist_session else None
    history = session.messages() if session is not None else []
    # Persist the user turn before making the first provider request.  The
    # previous implementation only wrote it when the session was empty, which
    # made resumed sessions silently lose every subsequent prompt.  Keeping
    # the append here also means a cancelled turn remains visible in the
    # transcript, matching append-only agent session semantics.
    user_message = message("user", prompt)
    if session is not None:
        session.append("user", prompt)
    messages = history + [user_message]
    if resources.system_prompt and not any(m.get("role") == "system" for m in messages):
        messages.insert(0, message("system", resources.system_prompt))
    max_iterations = int(config.get("max_iterations", config.get("max_turns", 20)))
    context_cfg = config.get("context")
    context_cfg = context_cfg if isinstance(context_cfg, Mapping) else {}
    max_tokens = max(
        1,
        int(context_cfg.get("max_tokens", config.get("max_context_tokens", 120000))),
    )
    compaction_cfg = config.get("compaction")
    compaction_cfg = compaction_cfg if isinstance(compaction_cfg, Mapping) else {}
    compaction_enabled = bool(compaction_cfg.get("enabled", True))
    threshold = compaction_cfg.get("threshold")
    compact_limit = max_tokens
    if isinstance(threshold, (int, float)) and not isinstance(threshold, bool):
        compact_limit = (
            max(1, int(max_tokens * threshold))
            if 0 < threshold <= 1
            else max(1, int(threshold))
        )
    preserve_recent_tokens = compaction_cfg.get("preserve_recent_tokens")
    if isinstance(preserve_recent_tokens, bool) or not isinstance(
        preserve_recent_tokens, (int, float)
    ):
        preserve_recent_tokens = None
    elif preserve_recent_tokens > 0:
        preserve_recent_tokens = int(preserve_recent_tokens)
    else:
        preserve_recent_tokens = None
    tail_turns = compaction_cfg.get("tail_turns")
    if isinstance(tail_turns, bool) or not isinstance(tail_turns, int):
        tail_turns = None
    elif tail_turns <= 0:
        tail_turns = 0
    prune_compaction = bool(compaction_cfg.get("prune", False))
    await _notify(
        events,
        on_event,
        RuntimeEvent(
            "agent_start",
            {
                "session_file": str(
                    session.path if session is not None else session_path
                ),
                "workspace": str(workspace),
            },
        ),
    )
    state["result"] = None
    iteration = -1
    try:
        for iteration in range(max_iterations):
            if cancel_event.is_set():
                raise asyncio.CancelledError
            if compaction_enabled:
                messages = compact(
                    messages,
                    max_tokens=compact_limit,
                    preserve_recent_tokens=preserve_recent_tokens,
                    tail_turns=tail_turns,
                    prune=prune_compaction,
                )
            await _notify(
                events,
                on_event,
                RuntimeEvent(
                    "model_start",
                    {"iteration": iteration, "message_count": len(messages)},
                ),
            )
            provider_task = asyncio.create_task(
                providers.complete(config, messages, schemas(), event_cb=on_event)
            )
            if cancel_event.is_set():
                provider_task.cancel()
                await asyncio.gather(provider_task, return_exceptions=True)
                raise asyncio.CancelledError
            cancel_wait = asyncio.create_task(cancel_event.wait())
            done, _ = await asyncio.wait(
                {provider_task, cancel_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)
            if cancel_event.is_set() and provider_task not in done:
                provider_task.cancel()
                await asyncio.gather(provider_task, return_exceptions=True)
                raise asyncio.CancelledError
            result = await provider_task
            for key, value in result.usage.items():
                if isinstance(value, (int, float)):
                    usage[key] = usage.get(key, 0) + value
            # A provider assistant turn is a single message.  When tool calls
            # are returned, text and calls belong to that same assistant
            # envelope; emitting a separate empty assistant message causes
            # OpenAI-compatible providers to reject the transcript and loses
            # Anthropic/Responses continuation metadata.
            assistant_calls = [
                {
                    "id": str(call.get("id") or f"call-{iteration}-{index}"),
                    "name": str(call.get("name", "")),
                    "arguments": call.get("arguments") or {},
                }
                for index, call in enumerate(result.tool_calls)
            ]
            assistant_message = message(
                "assistant",
                result.text or "",
                **({"tool_calls": assistant_calls} if assistant_calls else {}),
            )
            messages.append(assistant_message)
            truncated = str(result.stop_reason or "").lower() in {
                "length",
                "max_tokens",
                "max_output_tokens",
                "incomplete",
                "failed",
                "cancelled",
                "error",
            }
            if truncated:
                if session is not None:
                    session.append(
                        "assistant",
                        result.text or "",
                        tool_calls=assistant_calls,
                        stop_reason=result.stop_reason,
                    )
                if result.text:
                    await _notify(
                        events,
                        on_event,
                        RuntimeEvent(
                            "assistant_delta",
                            {"text": result.text, "iteration": iteration},
                        ),
                    )
                detail = f"OpenClaw provider stopped with {result.stop_reason}"
                await _notify(
                    events,
                    on_event,
                    RuntimeEvent(
                        "agent_end",
                        {
                            "stop_reason": result.stop_reason or "incomplete",
                            "iterations": iteration + 1,
                            "status": "error",
                        },
                    ),
                )
                final = {
                    "status": "error",
                    "output": result.text or "",
                    "events": events,
                    "usage": usage,
                    "error_message": detail,
                }
                state["result"] = final
                return final
            if result.text:
                await _notify(
                    events,
                    on_event,
                    RuntimeEvent(
                        "assistant_delta", {"text": result.text, "iteration": iteration}
                    ),
                )
            if not result.tool_calls:
                if session is not None:
                    session.append(
                        "assistant", result.text or "", stop_reason=result.stop_reason
                    )
                await _notify(
                    events,
                    on_event,
                    RuntimeEvent(
                        "agent_end",
                        {
                            "stop_reason": result.stop_reason or "stop",
                            "iterations": iteration + 1,
                        },
                    ),
                )
                output = result.text
                final = {
                    "status": "finished",
                    "output": output,
                    "events": events,
                    "usage": usage,
                }
                state["result"] = final
                return final
            if session is not None:
                session.append(
                    "assistant",
                    result.text or "",
                    tool_calls=assistant_calls,
                    stop_reason=result.stop_reason,
                )
            for index, call in enumerate(result.tool_calls):
                name = str(call.get("name", ""))
                args = call.get("arguments") or {}
                call_id = assistant_calls[index]["id"]
                await _notify(
                    events,
                    on_event,
                    RuntimeEvent(
                        "tool_start",
                        {
                            "id": call_id,
                            "name": name,
                            "arguments": args,
                            "iteration": iteration,
                        },
                    ),
                )
                try:
                    tool_result = await tools.execute(name, args)
                except PermissionError as exc:
                    tool_result = {
                        "ok": False,
                        "tool": name,
                        "error": str(exc),
                        "error_type": "permission_denied",
                    }
                except Exception as exc:
                    tool_result = {
                        "ok": False,
                        "tool": name,
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    }
                tool_content = _tool_output(name, tool_result, workspace)
                tool_message = message(
                    "tool",
                    tool_content,
                    tool_call_id=call_id,
                    name=name,
                )
                # Keep media attachments in the provider-neutral transcript.
                # The wire adapters decide whether to embed them in a native
                # tool result or inject the upstream synthetic user message.
                if isinstance(tool_result.get("attachments"), list):
                    tool_message["attachments"] = copy.deepcopy(
                        tool_result["attachments"]
                    )
                messages.append(tool_message)
                # Persist each result associated by its call id.  The
                # assistant envelope above is recorded once even when a turn
                # contains several calls.
                if session is not None:
                    session.append(
                        "tool",
                        tool_content,
                        tool_call_id=call_id,
                        name=name,
                        receipt=tool_result,
                    )
                await _notify(
                    events,
                    on_event,
                    RuntimeEvent(
                        "tool_end",
                        {
                            "id": call_id,
                            "name": name,
                            "result": tool_result,
                            "iteration": iteration,
                        },
                    ),
                )
        await _notify(
            events,
            on_event,
            RuntimeEvent(
                "agent_end",
                {
                    "stop_reason": "max_iterations",
                    "iterations": max_iterations,
                    "status": "error",
                },
            ),
        )
        result = {
            "status": "error",
            "output": "",
            "events": events,
            "usage": usage,
            "error_message": f"OpenClaw reached max_iterations={max_iterations}",
        }
        state["result"] = result
        return result
    except asyncio.CancelledError:
        # ``asyncio.wait_for`` and an outer task cancellation use task
        # cancellation, whereas the public ``cancel_event`` is a cooperative
        # request handled as a normal failed trajectory.  Let the former
        # propagate so the Target adapter can report a real timeout.
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            raise
        await _notify(
            events,
            on_event,
            RuntimeEvent(
                "agent_end",
                {
                    "stop_reason": "cancelled",
                    "iterations": iteration + 1,
                    "status": "error",
                },
            ),
        )
        result = {
            "status": "error",
            "output": "",
            "events": events,
            "usage": usage,
            "error_message": "OpenClaw run cancelled",
        }
        state["result"] = result
        return result
    except Exception as exc:
        await _notify(
            events,
            on_event,
            RuntimeEvent(
                "agent_end",
                {
                    "stop_reason": "error",
                    "iterations": iteration + 1,
                    "status": "error",
                },
            ),
        )
        result = {
            "status": "error",
            "output": "",
            "events": events,
            "usage": usage,
            "error_message": str(exc),
        }
        state["result"] = result
        return result
