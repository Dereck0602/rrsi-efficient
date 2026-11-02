"""Pi's non-interactive agent loop implemented without Node or npm.

The loop follows Pi's JSON-mode lifecycle and provider-neutral message model.
It intentionally uses ``httpx`` directly so the selected Snapshot contains all
execution logic and never imports the host's Agent implementation.

The reference behavior is Pi v1.4.0 (upstream commit
``4a080252f06caf63a525d7873c5d94d3c23887e0``); this module ports the
headless Target/Worker path and keeps unsupported interactive/runtime features
outside the contract.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx

from .anthropic_messages import anthropic_response, build_anthropic_payload
from .branch_summary import navigate
from .compaction import (
    branch_settings,
    content_text,
    convert_to_llm,
    is_retryable_summary_error,
    model_limits,
    model_metadata,
    retry_settings,
)
from .compaction import (
    settings as compaction_settings,
)
from .compaction_runtime import CompactionRuntime
from .controls import EventLog, declare_tool_changes, hook
from .controls import call as call_hook
from .controls import drain as drain_queue
from .openai_provider import build_payload as build_openai_payload
from .openai_provider import parse_arguments, request_headers, response_message
from .provider_options import clamp_max_tokens
from .provider_retry import ProviderRequestError, request_json
from .resources import (
    ResourceBundle,
    default_tools,
    expand_prompt_template,
    load_resources,
)
from .session import Session, current_system_message, system_message_text
from .stream_transport import request_stream
from .tools import ToolResult, _resolve, execute_tool, prepare_tool_arguments


@dataclass
class NativePiResult:
    status: str
    output: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)
    stop_reason: str | None = None
    error_message: str | None = None


@dataclass
class _ProviderReply:
    message: dict[str, Any]
    tool_calls: list[dict[str, Any]]
    usage: dict[str, Any]
    stop_reason: str | None
    raw: dict[str, Any]


def _model(config: Mapping[str, Any]) -> tuple[str, str]:
    llm = config.get("llm", {})
    value = llm.get("model_name")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Pi llm.model_name must be a non-empty string")
    value = value.strip()
    if "/" in value:
        provider, model = value.split("/", 1)
    else:
        provider, model = llm.get("provider_name"), value
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError(
            "Pi llm.model_name must use provider/model, or set llm.provider_name"
        )
    if not model.strip():
        raise ValueError("Pi llm.model_name must include a model id")
    return provider.strip().lower(), model.strip()


def _selected_tools(
    config: Mapping[str, Any], files_dir: Path | None = None
) -> list[str]:
    value = config.get("tools")
    if value is None:
        return (
            default_tools(files_dir)
            if files_dir is not None
            else ["read", "bash", "edit", "write"]
        )
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise ValueError("Pi tools must be a list of non-empty strings")
    available = {"read", "bash", "edit", "write", "grep", "find", "ls"}
    additive = any(item[:1] in {"+", "-"} for item in value)
    if additive and any(item[:1] not in {"+", "-"} for item in value):
        raise ValueError("Pi tools cannot mix plain names with +/- modifiers")
    configured_defaults = (
        default_tools(files_dir)
        if files_dir is not None
        else ["read", "bash", "edit", "write"]
    )
    selected: list[str] = list(configured_defaults) if additive else []
    for item in value:
        add = not item.startswith("-")
        name = item[1:] if item[:1] in {"+", "-"} else item
        if name not in available:
            raise ValueError(f"Pi unknown tool: {name}")
        if add and name not in selected:
            selected.append(name)
        elif not add and name in selected:
            selected.remove(name)
    if value and not additive:
        selected = list(dict.fromkeys(value))
    return selected


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            item.get("text", "")
            for item in content
            if isinstance(item, Mapping) and isinstance(item.get("text"), str)
        )
    return ""


def _usage(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result = dict(value)
    aliases = {
        "input_tokens": "input",
        "prompt_tokens": "input",
        "output_tokens": "output",
        "completion_tokens": "output",
    }
    for source, target in aliases.items():
        if source in result and target not in result:
            result[target] = result[source]
    return result


def _max_retries(config: Mapping[str, Any], llm: Mapping[str, Any]) -> int:
    value = config.get(
        "max_retries",
        config.get(
            "llm_max_retries",
            llm.get("max_retries", llm.get("llm_max_retries", 0)),
        ),
    )
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _reasoning_effort(config: Mapping[str, Any], llm: Mapping[str, Any]) -> str | None:
    value = config.get(
        "thinking",
        config.get(
            "reasoning_effort",
            llm.get("thinking", llm.get("reasoning_effort", llm.get("effort"))),
        ),
    )
    if value in (None, "", "off", "none"):
        return None
    return str(value)


def _pi_usage(value: Any) -> dict[str, Any]:
    """Normalize provider usage to Pi's shared ``Usage`` shape.

    Providers use different names for the same counters.  Preserve their
    original fields for diagnostics while adding the fields Pi consumers use
    (`input`, `output`, `totalTokens`, and `cost`).
    """

    raw_value = dict(value) if isinstance(value, Mapping) else {}
    raw = _usage(raw_value)
    input_tokens = raw.get("input", 0)
    output_tokens = raw.get("output", 0)
    input_details = raw.get("prompt_tokens_details") or raw.get("input_tokens_details")
    if not isinstance(input_details, Mapping):
        input_details = {}
    cache_read = raw.get(
        "cacheRead",
        raw.get(
            "cache_read_input_tokens",
            raw.get("cached_tokens", input_details.get("cached_tokens", 0)),
        ),
    )
    cache_write = raw.get(
        "cacheWrite",
        raw.get(
            "cache_creation_input_tokens",
            input_details.get("cache_creation_input_tokens", 0),
        ),
    )

    # OpenAI reports prompt/input tokens including the cached portion, while
    # Pi's ``Usage.input`` is the uncached count. Anthropic's
    # ``input_tokens`` is already uncached, so only subtract when the provider
    # exposes the OpenAI-style details or prompt counter.
    prompt_total = raw_value.get("prompt_tokens")
    input_total = raw_value.get("input_tokens")
    openai_cached = (
        isinstance(raw_value.get("prompt_tokens_details"), Mapping)
        or isinstance(raw_value.get("input_tokens_details"), Mapping)
        or "cached_tokens" in raw_value
    )

    def number(item: Any) -> int | float:
        return (
            item if isinstance(item, (int, float)) and not isinstance(item, bool) else 0
        )

    input_tokens = number(input_tokens)
    output_tokens = number(output_tokens)
    cache_read = number(cache_read)
    cache_write = number(cache_write)
    if isinstance(prompt_total, (int, float)) and not isinstance(prompt_total, bool):
        input_tokens = max(0, prompt_total - cache_read - cache_write)
    elif (
        isinstance(input_total, (int, float))
        and not isinstance(input_total, bool)
        and openai_cached
    ):
        input_tokens = max(0, input_total - cache_read - cache_write)
    result = dict(raw)
    result.update(
        {
            "input": input_tokens,
            "output": output_tokens,
            "cacheRead": cache_read,
            "cacheWrite": cache_write,
            "totalTokens": number(
                raw.get(
                    "totalTokens",
                    raw.get(
                        "total_tokens",
                        input_tokens + output_tokens + cache_read + cache_write,
                    ),
                )
            ),
            "cost": raw.get(
                "cost",
                {
                    "input": 0,
                    "output": 0,
                    "cacheRead": 0,
                    "cacheWrite": 0,
                    "total": 0,
                },
            ),
        }
    )
    return result


def _now_ms() -> int:
    return int(time.time() * 1000)


def _session_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _api_name(provider: str, config: Mapping[str, Any]) -> str:
    model_api = config.get("llm", {}).get("model_api")
    if model_api not in (None, ""):
        if not isinstance(model_api, str) or model_api not in {
            "openai-completions",
            "openai-responses",
            "anthropic-messages",
        }:
            raise ValueError(
                "Pi llm.model_api must be openai-completions, "
                "openai-responses, or anthropic-messages"
            )
        return model_api
    preferred = model_metadata(config).get("api")
    if preferred in {"openai-completions", "openai-responses", "anthropic-messages"}:
        return preferred
    if preferred:
        raise ValueError(
            f"Pi model API {preferred} is not implemented by this Snapshot"
        )
    if provider in {"anthropic", "anthropic-messages"}:
        return "anthropic-messages"
    return "openai-completions"


def _stop_reason(value: Any, *, has_tools: bool = False) -> str:
    """Map provider stop labels to Pi's AssistantMessage stopReason."""

    value = str(value or "")
    if value in {"length", "max_tokens", "max_output_tokens"}:
        return "length"
    if value in {"error", "failed", "incomplete", "content_filter"}:
        return "error"
    if value in {"aborted", "abort"}:
        return "aborted"
    # A provider may omit an explicit tool-call finish label while still
    # returning tool calls.  Preserve an explicit length label above: Pi
    # refuses to execute calls from a token-truncated assistant message.
    if value in {"tool_calls", "tool_use", "function_call"} or has_tools:
        return "toolUse"
    return "stop"


def _assistant_event_message(
    reply: _ProviderReply,
    *,
    provider: str,
    model: str,
    api: str,
    timestamp: int,
) -> dict[str, Any]:
    """Convert our provider-neutral reply to Pi's AssistantMessage shape."""

    content: list[dict[str, Any]] = []
    text = _message_text(reply.message)
    if text:
        content.append({"type": "text", "text": text})
    for call in reply.tool_calls:
        try:
            arguments = json.loads(call.get("arguments", "{}"))
        except (TypeError, json.JSONDecodeError):
            arguments = {}
        content.append(
            {
                "type": "toolCall",
                "id": call.get("id", ""),
                "name": call.get("name", ""),
                "arguments": arguments if isinstance(arguments, dict) else {},
            }
        )
    usage = _pi_usage(reply.usage)
    message = {
        "role": "assistant",
        "content": content,
        "api": api,
        "provider": provider,
        "model": model,
        "usage": usage,
        "stopReason": _stop_reason(reply.stop_reason, has_tools=bool(reply.tool_calls)),
        "rawStopReason": reply.stop_reason,
        "timestamp": timestamp,
    }
    if message["stopReason"] == "error":
        error = reply.raw.get("error") or reply.raw.get("errorMessage")
        message["errorMessage"] = (
            error.get("message", str(error))
            if isinstance(error, Mapping)
            else str(error or "Unknown error")
        )
    if "_pi_content" in reply.raw:
        message["content"] = copy.deepcopy(reply.raw["_pi_content"])
    if reply.raw.get("_provider_thinking_level"):
        message["providerThinkingLevel"] = reply.raw["_provider_thinking_level"]
    if reply.raw.get("_pi_message"):
        message = {**copy.deepcopy(reply.raw["_pi_message"]), "timestamp": timestamp}
    return message


def _openai_url(base_url: str) -> str:
    url = base_url.rstrip("/")
    return url if url.endswith("/chat/completions") else url + "/chat/completions"


def _headers(
    config: Mapping[str, Any], defaults: dict[str, str], generated=None
) -> dict[str, str]:
    value = config.get("llm", {}).get("headers", config.get("headers", {}))
    if not isinstance(value, Mapping) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError("Pi llm.headers must map strings to strings")
    result = dict(defaults)
    for layer in (model_metadata(config).get("headers") or {}, generated or {}, value):
        for key, item in layer.items():
            result = {
                name: data
                for name, data in result.items()
                if name.lower() != key.lower()
            }
            if item is not None:
                result[key] = item
    return result


async def _request_provider(
    client,
    url,
    headers,
    payload,
    retries,
    *,
    config,
    api,
    context,
    managed_effort=None,
    on_event=None,
    cancel_event=None,
    diagnostics=None,
    **options,
):
    if not hasattr(client, "stream"):
        return await request_json(client, url, headers, payload, retries, **options)
    if api == "anthropic-messages":
        from .anthropic_stream import AnthropicStream

        parser = AnthropicStream(
            config, context, managed_effort, diagnostics=diagnostics
        )
    else:
        from .openai_stream import OpenAIStream

        parser = OpenAIStream(config, api, context, managed_effort)
    payload = {**payload, "stream": True}
    if api == "openai-completions":
        if (
            model_metadata(config).get("compat", {}).get("supportsUsageInStreaming")
            is not False
        ):
            payload["stream_options"] = {"include_usage": True}
    return await request_stream(
        client,
        url,
        headers,
        payload,
        retries,
        parser=parser,
        on_event=on_event,
        cancel_event=cancel_event,
        **options,
    )


async def _openai_call(
    client: httpx.AsyncClient,
    config: Mapping[str, Any],
    provider: str,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    context_messages=None,
    on_event=None,
    cancel_event=None,
) -> _ProviderReply:
    llm = config["llm"]
    base_url = str(
        llm.get("base_url")
        or model_metadata(config).get("baseUrl")
        or "https://api.openai.com/v1"
    )
    context = context_messages if context_messages is not None else messages
    payload, _ = build_openai_payload(
        config, context, "openai-completions", _reasoning_effort(config, llm), tools
    )
    extra_body = llm.get("extra_body", config.get("extra_body"))
    if isinstance(extra_body, Mapping):
        payload.update(extra_body)
    data = await _request_provider(
        client,
        _openai_url(base_url),
        _headers(
            config,
            {
                "Authorization": f"Bearer {llm['api_key']}",
                "Content-Type": "application/json",
            },
            request_headers(config, "openai-completions"),
        ),
        payload,
        _max_retries(config, llm),
        timeout=config.get("_provider_timeout"),
        max_retry_delay=config.get("_provider_retry_delay", 60.0),
        config=config,
        api="openai-completions",
        context=context,
        on_event=on_event,
        cancel_event=cancel_event,
    )
    if not data.get("_pi_message") and not data.get("choices"):
        raise RuntimeError("OpenAI-compatible response has no choices")
    canonical = data.get("_pi_message") or response_message(
        config, data, "openai-completions", context=context
    )
    data["_pi_message"] = canonical
    calls = [
        {
            "id": b["id"],
            "name": b["name"],
            "arguments": json.dumps(b["arguments"], ensure_ascii=False),
            **(
                {"argumentsError": b["argumentsError"]}
                if b.get("argumentsError")
                else {}
            ),
        }
        for b in canonical["content"]
        if b["type"] == "toolCall"
    ]
    return _ProviderReply(
        {"role": "assistant", "content": content_text(canonical["content"], "")},
        calls,
        canonical["usage"],
        canonical["stopReason"],
        data,
    )


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            continue
        if role == "tool":
            result.append(
                {
                    "type": "function_call_output",
                    "call_id": message.get("tool_call_id", ""),
                    "output": message.get("content", ""),
                }
            )
            continue
        if role == "assistant" and message.get("tool_calls"):
            if message.get("content"):
                result.append({"role": "assistant", "content": message["content"]})
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                result.append(
                    {
                        "type": "function_call",
                        "call_id": call.get("id", ""),
                        "name": function.get("name", ""),
                        "arguments": function.get("arguments", "{}"),
                    }
                )
            continue
        result.append({"role": role, "content": message.get("content", "")})
    return result


async def _openai_responses_call(
    client: httpx.AsyncClient,
    config: Mapping[str, Any],
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    context_messages=None,
    on_event=None,
    cancel_event=None,
) -> _ProviderReply:
    llm = config["llm"]
    base_url = str(
        llm.get("base_url")
        or model_metadata(config).get("baseUrl")
        or "https://api.openai.com/v1"
    ).rstrip("/")
    url = base_url if base_url.endswith("/responses") else base_url + "/responses"
    context = context_messages if context_messages is not None else messages
    payload, managed_effort = build_openai_payload(
        config, context, "openai-responses", _reasoning_effort(config, llm), tools
    )
    extra_body = llm.get("extra_body", config.get("extra_body"))
    if isinstance(extra_body, Mapping):
        payload.update(extra_body)
    data = await _request_provider(
        client,
        url,
        _headers(
            config,
            {
                "Authorization": f"Bearer {llm['api_key']}",
                "Content-Type": "application/json",
            },
            request_headers(config, "openai-responses"),
        ),
        payload,
        _max_retries(config, llm),
        timeout=config.get("_provider_timeout"),
        max_retry_delay=config.get("_provider_retry_delay", 60.0),
        config=config,
        api="openai-responses",
        context=context,
        on_event=on_event,
        cancel_event=cancel_event,
        managed_effort=managed_effort,
    )
    canonical = data.get("_pi_message") or response_message(
        config, data, "openai-responses", managed_effort=managed_effort, context=context
    )
    data["_pi_message"] = canonical
    calls = [
        {
            "id": b["id"],
            "name": b["name"],
            "arguments": json.dumps(b["arguments"], ensure_ascii=False),
            **(
                {"argumentsError": b["argumentsError"]}
                if b.get("argumentsError")
                else {}
            ),
        }
        for b in canonical["content"]
        if b["type"] == "toolCall"
    ]
    return _ProviderReply(
        {"role": "assistant", "content": content_text(canonical["content"], "")},
        calls,
        canonical["usage"],
        canonical["stopReason"],
        data,
    )


def _anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id", ""),
                "content": message.get("content", ""),
            }
            if result and result[-1].get("role") == "user":
                content = result[-1].setdefault("content", [])
                if isinstance(content, list):
                    content.append(block)
            else:
                result.append({"role": "user", "content": [block]})
            continue
        if role == "assistant" and message.get("tool_calls"):
            content: list[dict[str, Any]] = []
            if message.get("content"):
                content.append({"type": "text", "text": message["content"]})
            for call in message["tool_calls"]:
                function = call.get("function") or {}
                try:
                    arguments = json.loads(function.get("arguments", "{}"))
                except (TypeError, json.JSONDecodeError):
                    arguments = {}
                content.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id", ""),
                        "name": function.get("name", ""),
                        "input": arguments,
                    }
                )
            result.append({"role": "assistant", "content": content})
        else:
            result.append({"role": role, "content": message.get("content", "")})
    return result


async def _anthropic_call(
    client: httpx.AsyncClient,
    config: Mapping[str, Any],
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    *,
    context_messages: list[dict[str, Any]] | None = None,
    on_event=None,
    cancel_event=None,
) -> _ProviderReply:
    llm = config["llm"]
    base_url = str(
        llm.get("base_url")
        or model_metadata(config).get("baseUrl")
        or "https://api.anthropic.com"
    )
    url = base_url.rstrip("/")
    if not url.endswith("/messages"):
        url += "/messages" if url.endswith("/v1") else "/v1/messages"
    context = messages if context_messages is None else context_messages
    payload, betas, managed_effort = build_anthropic_payload(
        config,
        context,
        _reasoning_effort(config, llm),
        tools_override=tools,
        cache_retention=config.get("cache_retention", "short"),
    )
    headers = _headers(
        config,
        {
            **(
                {
                    "Authorization": f"Bearer {llm['api_key']}",
                    "user-agent": "claude-cli/2.1.280",
                    "x-app": "cli",
                }
                if "sk-ant-oat" in str(llm["api_key"])
                else {"x-api-key": str(llm["api_key"])}
            ),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            **({"anthropic-beta": ",".join(betas)} if betas else {}),
        },
    )
    headers = {
        key: value for key, value in headers.items() if key.lower() != "anthropic-beta"
    }
    if betas:
        headers["anthropic-beta"] = ",".join(betas)
    diagnostic = None
    try:
        data = await _request_provider(
            client,
            url,
            headers,
            payload,
            _max_retries(config, llm),
            timeout=config.get("_provider_timeout"),
            max_retry_delay=config.get("_provider_retry_delay", 60.0),
            config=config,
            api="anthropic-messages",
            context=context,
            on_event=on_event,
            cancel_event=cancel_event,
            managed_effort=managed_effort,
        )
    except ProviderRequestError as exc:
        try:
            error_body = json.loads(exc.body or "{}")
        except ValueError:
            error_body = {}
        error = error_body.get("error") or {}
        preferred_names = {
            tool["name"]
            for message in context
            if message.get("role") == "system"
            for tool in message.get("toolsAdded", [])
            if (tool.get("constrainedSampling") or {}).get("type") == "json_schema"
            and (tool.get("constrainedSampling") or {}).get("strict") == "prefer"
        }
        preferred_names.update(
            tool["name"]
            for definition in tools
            for tool in (definition.get("function", definition),)
            if (tool.get("constrainedSampling") or {}).get("type") == "json_schema"
            and (tool.get("constrainedSampling") or {}).get("strict") == "prefer"
        )

        # Only a preferred declaration actually sent with strict:true can
        # trigger the upstream one-time schema downgrade. Native additions
        # can be nested inside conversation blocks instead of payload.tools.
        def has_preferred_strict(value):
            if isinstance(value, list):
                return any(has_preferred_strict(item) for item in value)
            if not isinstance(value, Mapping):
                return False
            return (
                value.get("strict") is True
                and (
                    value.get("name") in preferred_names
                    or str(value.get("name", "")).lower() in preferred_names
                )
            ) or any(has_preferred_strict(item) for item in value.values())

        preferred = has_preferred_strict(payload)
        if not (
            exc.status == 400
            and error.get("type") == "invalid_request_error"
            and preferred
            and any(
                needle in str(error.get("message", ""))
                for needle in (
                    "The compiled grammar is too large",
                    "Too many strict tools (",
                    "Schemas contains too many parameters with union types (",
                )
            )
        ):
            raise
        payload, _, managed_effort = build_anthropic_payload(
            config,
            context,
            _reasoning_effort(config, llm),
            tools_override=tools,
            cache_retention=config.get("cache_retention", "short"),
            disable_preferred_strict=True,
        )
        diagnostic = {
            "type": "anthropic_strict_tools_fallback",
            "timestamp": _now_ms(),
            "error": {"message": f"400 {exc.body}"},
            "details": {"reason": "strict_tool_limits_exceeded"},
        }
        data = await _request_provider(
            client,
            url,
            headers,
            payload,
            _max_retries(config, llm),
            timeout=config.get("_provider_timeout"),
            max_retry_delay=config.get("_provider_retry_delay", 60.0),
            config=config,
            api="anthropic-messages",
            context=context,
            on_event=on_event,
            cancel_event=cancel_event,
            managed_effort=managed_effort,
            diagnostics=[diagnostic],
        )
    canonical = data.get("_pi_message") or anthropic_response(
        config, data, context=context, managed_effort=managed_effort
    )
    if diagnostic and diagnostic not in canonical.get("diagnostics", []):
        canonical.setdefault("diagnostics", []).append(diagnostic)
    data["_pi_message"] = canonical
    data["_provider_thinking_level"] = managed_effort
    calls = [
        {
            "id": b["id"],
            "name": b["name"],
            "arguments": json.dumps(b["arguments"], ensure_ascii=False),
            **(
                {"argumentsError": b["argumentsError"]}
                if b.get("argumentsError")
                else {}
            ),
        }
        for b in canonical["content"]
        if b["type"] == "toolCall"
    ]
    return _ProviderReply(
        {"role": "assistant", "content": content_text(canonical["content"], "")},
        calls,
        canonical["usage"],
        canonical["stopReason"],
        data,
    )


def _event(events: list[dict[str, Any]], value: dict[str, Any]) -> None:
    events.append(value)


def _accumulate_usage(usage: dict[str, Any], provider_usage: Mapping[str, Any]) -> None:
    usage["model_calls"] += 1
    for key in ("input", "output"):
        value = provider_usage.get(key, 0)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            usage[key] += value
    usage["input_tokens"] = usage["input"]
    usage["output_tokens"] = usage["output"]
    usage["cache_read_input_tokens"] += provider_usage.get("cacheRead", 0)
    usage["cache_creation_input_tokens"] += provider_usage.get("cacheWrite", 0)
    usage["total_tokens"] = sum(
        usage[key]
        for key in (
            "input",
            "output",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    )


class _ToolBatch(list):
    terminate = False


def _tool_payload(result):
    value = {
        "content": result.content_blocks
        if result.content_blocks is not None
        else [{"type": "text", "text": result.content}],
        "details": result.details,
    }
    for attribute, key in (
        ("structured_content", "structuredContent"),
        ("usage", "usage"),
        ("terminate", "terminate"),
    ):
        item = getattr(result, attribute, None)
        if item is not None:
            value[key] = item
    return value


async def _flush(events):
    if isinstance(events, EventLog):
        await events.flush()


async def _execute_tool_batch(
    calls,
    *,
    selected,
    cwd,
    events,
    usage,
    stop_reason,
    mode,
    cancel_event=None,
    model=None,
    auto_resize_images=True,
    hooks=None,
    assistant_message=None,
    context=None,
):
    """Source-order preparation; completion-order ends; source-order artifacts."""
    mutation_locks = {}
    event_loop = asyncio.get_running_loop()
    finalized = []

    def canonical_call(call):
        args, _ = parse_arguments(call.get("arguments", "{}"))
        return {
            "type": "toolCall",
            "id": call["id"],
            "name": call.get("name", ""),
            "arguments": args if isinstance(args, dict) else {},
        }

    async def prepare(call):
        usage["tool_calls"] += 1
        block = canonical_call(call)
        _event(
            events,
            {
                "type": "tool_execution_start",
                "toolCallId": call["id"],
                "toolName": block["name"],
                "args": block["arguments"],
            },
        )
        await _flush(events)
        if stop_reason == "length":
            return None, ToolResult(
                f'Tool call "{block["name"]}" was not executed: the response hit the '
                "output token limit, so its arguments may be truncated. "
                "Re-issue the tool call with complete arguments.",
                is_error=True,
            )
        if block["name"] not in selected:
            return None, ToolResult(f"Tool {block['name']} not found", is_error=True)
        arguments, arguments_error = parse_arguments(call.get("arguments", "{}"))
        try:
            if not isinstance(arguments, dict):
                raise ValueError("Tool arguments must be a valid JSON object.")
            arguments = prepare_tool_arguments(
                block["name"], arguments, call.get("argumentsError") or arguments_error
            )
            before = await call_hook(
                hook(hooks, "before_tool_call"),
                {
                    "assistantMessage": assistant_message,
                    "toolCall": block,
                    "args": arguments,
                    "context": context,
                },
                cancel_event,
            )
            if cancel_event is not None and cancel_event.is_set():
                return None, ToolResult("Operation aborted", is_error=True)
            if before and before.get("block"):
                result = ToolResult(
                    before.get("reason") or "Tool execution was blocked", is_error=True
                )
                if before.get("terminate") is True:
                    result.terminate = True
                return None, result
            return arguments, None
        except Exception as exc:
            return None, ToolResult(str(exc), is_error=True)

    async def finish(call, result, duration=None):
        payload = _tool_payload(result)
        message = {
            "role": "toolResult",
            "toolCallId": call["id"],
            "toolName": call.get("name", ""),
            "content": payload["content"],
            "details": payload["details"],
            "isError": result.is_error,
            "timestamp": _now_ms(),
        }
        if "usage" in payload:
            message["usage"] = payload["usage"]
        end = {
            "type": "tool_execution_end",
            "toolCallId": call["id"],
            "toolName": call.get("name", ""),
            "result": payload,
            "isError": result.is_error,
        }
        if duration is not None:
            message["durationMs"] = end["durationMs"] = duration
        finalized.append(payload.get("terminate") is True)
        _event(events, end)
        await _flush(events)
        return message

    async def execute(call, arguments):
        started = time.monotonic()
        updates = []
        accepting = True

        async def emit_update(partial):
            _event(
                events,
                {
                    "type": "tool_execution_update",
                    "toolCallId": call["id"],
                    "toolName": call.get("name", ""),
                    "args": canonical_call(call)["arguments"],
                    "partialResult": _tool_payload(partial),
                },
            )
            await _flush(events)

        def update(partial):
            if accepting:
                updates.append(
                    asyncio.run_coroutine_threadsafe(emit_update(partial), event_loop)
                )

        def invoke():
            options = {}
            if cancel_event is not None:
                options["cancel_event"] = cancel_event
            if model is not None:
                options.update(model=model, auto_resize_images=auto_resize_images)
            if "on_update" in inspect.signature(execute_tool).parameters:
                options["on_update"] = update
            return execute_tool(call.get("name", ""), arguments, cwd, **options)

        async def worker():
            task = asyncio.create_task(asyncio.to_thread(invoke))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise

        try:
            if call.get("name") in {"write", "edit"}:
                path = _resolve(arguments.get("path"), cwd)
                lock = mutation_locks.setdefault(path, asyncio.Lock())
                async with lock:
                    result = await worker()
            else:
                result = await worker()
        except Exception as exc:
            result = ToolResult(str(exc), is_error=True)
        finally:
            accepting = False
            if updates:
                await asyncio.gather(
                    *(asyncio.wrap_future(future) for future in updates)
                )
        duration = round((time.monotonic() - started) * 1000)
        try:
            after = await call_hook(
                hook(hooks, "after_tool_call"),
                {
                    "assistantMessage": assistant_message,
                    "toolCall": canonical_call(call),
                    "args": arguments,
                    "result": _tool_payload(result),
                    "isError": result.is_error,
                    "context": context,
                },
                cancel_event,
            )
            if after:
                result = copy.copy(result)
                if "content" in after and after["content"] is not None:
                    result.content_blocks = after["content"]
                    result.structured_content = None
                for key, attribute in (
                    ("details", "details"),
                    ("structuredContent", "structured_content"),
                    ("usage", "usage"),
                    ("terminate", "terminate"),
                    ("isError", "is_error"),
                ):
                    if after.get(key) is not None:
                        setattr(result, attribute, after[key])
        except Exception as exc:
            result = ToolResult(str(exc), is_error=True)
        return await finish(call, result, duration)

    async def emit_result(message):
        _event(events, {"type": "message_start", "message": message})
        _event(events, {"type": "message_end", "message": message})
        await _flush(events)

    results = _ToolBatch()
    if mode == "sequential" or stop_reason == "length":
        for call in calls:
            arguments, immediate = await prepare(call)
            message = (
                await finish(call, immediate)
                if immediate is not None
                else await execute(call, arguments)
            )
            results.append(message)
            await emit_result(message)
            if cancel_event is not None and cancel_event.is_set():
                break
    else:
        prepared = []
        for call in calls:
            arguments, immediate = await prepare(call)
            prepared.append(
                (
                    call,
                    arguments,
                    await finish(call, immediate) if immediate is not None else None,
                )
            )
            if cancel_event is not None and cancel_event.is_set():
                break

        async def complete(call, arguments, immediate):
            if immediate is None and cancel_event is not None and cancel_event.is_set():
                return await finish(
                    call, ToolResult("Operation aborted", is_error=True)
                )
            return (
                immediate if immediate is not None else await execute(call, arguments)
            )

        results.extend(await asyncio.gather(*(complete(*item) for item in prepared)))
        for message in results:
            await emit_result(message)
    results.terminate = bool(finalized) and all(finalized)
    return results


def _transport_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert the canonical Pi transcript at the provider boundary only."""
    result: list[dict[str, Any]] = []
    converted = convert_to_llm(messages)
    current_system = current_system_message(converted)
    if current_system:
        result.append(
            {
                "role": "system",
                "content": system_message_text(current_system),
            }
        )
    for message in converted:
        role = message.get("role")
        if role == "system":
            continue
        if role == "toolResult":
            result.append(
                {
                    "role": "tool",
                    "tool_call_id": message["toolCallId"],
                    "content": content_text(message.get("content", []), ""),
                }
            )
        elif role == "assistant":
            if message.get("stopReason") in {"error", "aborted"}:
                continue
            value: dict[str, Any] = {
                "role": "assistant",
                "content": content_text(message.get("content", []), ""),
            }
            calls = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {
                        "name": b["name"],
                        "arguments": json.dumps(
                            b.get("arguments", {}), ensure_ascii=False
                        ),
                    },
                }
                for b in message.get("content", [])
                if b.get("type") == "toolCall"
            ]
            if calls:
                value["tool_calls"] = calls
            result.append(value)
        else:
            # Text-only user content stays the same shape as the original
            # headless adapter; canonical messages retain image blocks.
            result.append(
                {"role": role, "content": content_text(message.get("content", ""), "")}
            )
    return result


async def _provider_call(
    client,
    config,
    provider,
    model,
    messages,
    tools,
    *,
    context_messages=None,
    on_event=None,
    cancel_event=None,
):
    api = _api_name(provider, config)
    if api == "anthropic-messages":
        return await _anthropic_call(
            client,
            config,
            model,
            messages,
            tools,
            context_messages=context_messages,
            on_event=on_event,
            cancel_event=cancel_event,
        )
    if api == "openai-responses":
        return await _openai_responses_call(
            client,
            config,
            model,
            messages,
            tools,
            context_messages=context_messages,
            on_event=on_event,
            cancel_event=cancel_event,
        )
    return await _openai_call(
        client,
        config,
        provider,
        model,
        messages,
        tools,
        context_messages=context_messages,
        on_event=on_event,
        cancel_event=cancel_event,
    )


async def _run(
    config: Mapping[str, Any],
    prompt: str,
    working_dir: Path,
    files_dir: Path,
    resources: ResourceBundle | None = None,
    *,
    cancel_compaction: asyncio.Event | None = None,
    cancel_event: asyncio.Event | None = None,
    on_event=None,
    steering_queue=None,
    follow_up_queue=None,
    hooks=None,
    _state: dict | None = None,
) -> NativePiResult:
    """Run a Pi-compatible task using only Python and the selected Snapshot."""
    provider, model = _model(config)
    api = _api_name(provider, config)
    llm = config.get("llm", {})
    api_key = llm.get("api_key")
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("Pi requires the framework role's llm.api_key")
    # Validate new runtime controls before opening or changing a session file.
    options = compaction_settings(config, files_dir)
    retry_policy = retry_settings(config, files_dir)
    branch_options = branch_settings(config, files_dir)
    context_window, model_max_tokens = model_limits(config)
    config = copy.deepcopy(dict(config))
    settings_path = files_dir / ".pi" / "agent" / "settings.json"
    if "thinking_budgets" not in config and "thinkingBudgets" not in config:
        try:
            snapshot_settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            snapshot_settings = {}
        if isinstance(snapshot_settings, Mapping) and isinstance(
            snapshot_settings.get("thinkingBudgets"), Mapping
        ):
            config["thinking_budgets"] = copy.deepcopy(
                snapshot_settings["thinkingBudgets"]
            )
    provider_policy = retry_policy.get("provider", {})
    has_retries = any(
        key in config or key in llm for key in ("max_retries", "llm_max_retries")
    )
    if "maxRetries" in provider_policy and not has_retries:
        config["max_retries"] = provider_policy["maxRetries"]
    if (
        "timeoutMs" in provider_policy
        and "request_timeout" not in config
        and "timeout" not in config
    ):
        config["_provider_timeout"] = provider_policy["timeoutMs"] / 1000
    if "maxRetryDelayMs" in provider_policy:
        config["_provider_retry_delay"] = provider_policy["maxRetryDelayMs"] / 1000
    selected = _selected_tools(config, files_dir)
    resources = resources or load_resources(
        files_dir,
        working_dir,
        selected_tools=selected,
        append_system_prompt=str(config.get("system_prompt", "")),
        custom_system_prompt=config.get("custom_system_prompt"),
        force_system_prompt=config.get("force_system_prompt"),
        hidden_tools=config.get("hidden_tools", ()),
        prompt_guidelines=config.get("prompt_guidelines", ()),
        sections=config.get("sections"),
        project_trusted=config.get("project_trusted", True),
    )
    prompt = expand_prompt_template(prompt, resources.templates)
    session_options = config.get("session", {})
    if not isinstance(session_options, Mapping) or set(session_options) - {
        "file",
        "leaf_id",
    }:
        raise ValueError("Pi session accepts file and leaf_id")
    session_file = config.get(
        "session_file",
        session_options.get("file"),
    )
    if session_file is not None and not isinstance(session_file, (str, Path)):
        raise ValueError("Pi session_file must be a path")
    if session_file is not None:
        session_file = Path(session_file).expanduser()
        if not session_file.is_absolute():
            session_file = working_dir / session_file
    session = Session(
        working_dir,
        session_file,
        config.get("session_leaf_id", session_options.get("leaf_id")),
    )
    config["_session_id"] = session.header["id"]
    manual = prompt == "/compact" or prompt.startswith("/compact ")
    tree_command = prompt == "/tree" or prompt.startswith("/tree ")
    tree_parts = prompt.split(maxsplit=2) if tree_command else []
    target_id = (
        (tree_parts[1] if len(tree_parts) > 1 else None)
        if tree_command
        else config.get("session_target_id")
    )
    if tree_command and target_id is None:
        raise ValueError("Pi /tree requires a session entry id")
    system_message = {
        "role": "system",
        "content": ""
        if resources.system_sections is not None
        else resources.system_prompt,
        **(
            {"sections": resources.system_sections}
            if resources.system_sections is not None
            else {}
        ),
        "timestamp": _now_ms(),
    }
    if config.get("_worker_system_prompt"):
        system_message["sections"] = {
            **system_message.get("sections", {}),
            "flashrsi_worker": config["_worker_system_prompt"],
        }
    declarations = [dict(schema["function"]) for schema in resources.tool_schemas]
    if declarations:
        system_message["toolsAdded"] = declarations
    events: list[dict[str, Any]] = EventLog(on_event)
    usage: dict[str, Any] = {
        "input": 0,
        "output": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "model_calls": 0,
        "tool_calls": 0,
    }
    _event(events, dict(session.header))
    max_turns = config.get("max_turns")
    if max_turns is not None and (type(max_turns) is not int or max_turns < 1):
        raise ValueError("Pi max_turns must be a positive integer")
    tool_execution = config.get(
        "tool_execution", config.get("toolExecution", "parallel")
    )
    if tool_execution not in {"parallel", "sequential"}:
        raise ValueError("Pi tool_execution must be parallel or sequential")
    timeout = float(config.get("request_timeout", config.get("timeout", 300)))
    tool_schemas = resources.tool_schemas
    # ``runAgentLoop`` reports the initial user prompt in the new-message list;
    # keep the same transcript shape for callers consuming ``agent_end``.
    generated_messages: list[dict[str, Any]] = []
    user_message = {"role": "user", "content": prompt, "timestamp": _now_ms()}
    user_emitted = False
    if _state is not None:
        _state.update(
            session=session,
            events=events,
            usage=usage,
            generated_messages=generated_messages,
            user_emitted=False,
            provider=provider,
            model=model,
            api=api,
        )
    async with httpx.AsyncClient(timeout=timeout) as client:

        async def summarize(summary_messages, max_tokens, *, branch=False):
            summary_config = copy.deepcopy(dict(config))
            summary_config["cache_retention"] = "none"
            summary_config["_cache_retention"] = "none"
            summary_config["max_tokens"] = clamp_max_tokens(
                context_window, summary_messages, max_tokens
            )
            # Pi's one-off summary request uses its summarization prompt, no
            # tools, and no conversational temperature/body overrides.
            summary_config.pop("temperature", None)
            summary_config.pop("extra_body", None)
            summary_config["llm"].pop("temperature", None)
            summary_config["llm"].pop("extra_body", None)
            # Branch summaries never inherit a selected thinking level. Normal
            # compaction passes it only for models marked as reasoning models.
            if branch or not model_metadata(config).get("reasoning"):
                for key in ("thinking", "reasoning_effort", "effort"):
                    summary_config.pop(key, None)
                    summary_config["llm"].pop(key, None)
            reply = await _provider_call(
                client,
                summary_config,
                provider,
                model,
                summary_messages,
                [],
                context_messages=summary_messages,
            )
            value = _assistant_event_message(
                reply, provider=provider, model=model, api=api, timestamp=_now_ms()
            )
            _accumulate_usage(usage, value["usage"])
            usage["summary_calls"] = usage.get("summary_calls", 0) + 1
            usage["summary_input_tokens"] = (
                usage.get("summary_input_tokens", 0) + value["usage"]["input"]
            )
            usage["summary_output_tokens"] = (
                usage.get("summary_output_tokens", 0) + value["usage"]["output"]
            )
            return value

        async def summarize_branch(summary_messages, max_tokens):
            return await summarize(summary_messages, max_tokens, branch=True)

        compactor = CompactionRuntime(
            session,
            options,
            context_window,
            model_max_tokens,
            events,
            summarize,
            cancel_compaction,
            retry_policy,
        )
        retry_attempt = 0

        def will_retry(message):
            return (
                retry_policy["enabled"]
                and retry_attempt < retry_policy["maxRetries"]
                and message.get("stopReason") == "error"
                and not compactor.recovery(message)
                and is_retryable_summary_error(message.get("errorMessage", ""))
            )

        async def retry_turn(message, entry_id):
            nonlocal retry_attempt
            if not will_retry(message):
                if retry_attempt:
                    _event(
                        events,
                        {
                            "type": "auto_retry_end",
                            "success": False,
                            "attempt": retry_attempt,
                            "finalError": message.get("errorMessage", "Unknown error"),
                        },
                    )
                    retry_attempt = 0
                return False
            retry_attempt += 1
            delay = min(
                retry_policy["baseDelayMs"] * (2 ** (retry_attempt - 1)),
                retry_policy["maxAgentDelayMs"],
            )
            _event(
                events,
                {
                    "type": "auto_retry_start",
                    "attempt": retry_attempt,
                    "maxAttempts": retry_policy["maxRetries"],
                    "delayMs": delay,
                    "errorMessage": message.get("errorMessage", "Unknown error"),
                },
            )
            session.append("context_edit", targetId=entry_id, replacement=None)
            await _flush(events)
            await asyncio.sleep(delay / 1000)
            return True

        if target_id is not None:
            try:
                navigation = await navigate(
                    session,
                    str(target_id),
                    summarize_branch,
                    summarize_branch=branch_options.get("enabled", True),
                    context_window=context_window,
                    model_max_tokens=model_max_tokens,
                    reserve_tokens=branch_options.get("reserveTokens", 16384),
                    custom_instructions=(
                        tree_parts[2]
                        if len(tree_parts) > 2
                        else branch_options.get("customInstructions")
                    ),
                    replace_instructions=branch_options.get(
                        "replaceInstructions", False
                    ),
                    label=branch_options.get("label"),
                    cancel_event=cancel_compaction,
                    retry_policy=retry_policy,
                    events=events,
                )
            except Exception as exc:
                return NativePiResult(
                    "error", events=events, usage=usage, error_message=str(exc)
                )
            _event(
                events,
                {"type": "session_tree", "targetId": str(target_id), **navigation},
            )
            if navigation.get("cancelled"):
                return NativePiResult(
                    "error",
                    events=events,
                    usage=usage,
                    error_message="Branch summarization cancelled",
                )
            if tree_command:
                if session.entries and session.leaf_id != session.entries[-1].get("id"):
                    # Persist a branch selection even when no summary was
                    # requested, since each headless invocation is a fresh
                    # process. Pi's ordinary custom metadata stays invisible.
                    session.append(
                        "custom",
                        customType="native_branch_selection",
                        data={"targetId": str(target_id)},
                    )
                return NativePiResult(
                    "finished",
                    events=events,
                    usage=usage,
                    output=navigation.get("summaryEntry", {}).get(
                        "summary", navigation.get("editorText", "")
                    ),
                )
        if manual:
            compacted = await compactor.compact(
                "manual", focus=prompt[len("/compact") :].strip() or None
            )
            error = (
                None
                if compacted
                else next(
                    (
                        e.get("errorMessage")
                        for e in reversed(events)
                        if e["type"] == "compaction_end"
                    ),
                    "Compaction cancelled",
                )
            )
            return NativePiResult(
                "finished" if compacted else "error",
                output=compacted["summary"] if compacted else "",
                events=events,
                usage=usage,
                error_message=error or (None if compacted else "Compaction cancelled"),
            )
        previous_system = current_system_message(session.messages())
        worker_changed = (previous_system or {}).get("sections", {}).get(
            "flashrsi_worker"
        ) != config.get("_worker_system_prompt")
        if previous_system is not None and (
            resources.system_sections is not None or worker_changed
        ):
            sections = system_message.get("sections", {})
            system_message["sections"] = {
                **sections,
                **{
                    name: None
                    for name in previous_system.get("sections", {})
                    if name not in sections
                },
            }
        prompt_known = (
            previous_system is not None
            and resources.system_prompt in system_message_text(previous_system)
        )
        if (
            previous_system is None
            or not prompt_known
            or worker_changed
            or previous_system.get("toolsAdded", []) != declarations
        ):
            if prompt_known:
                system_message["content"] = ""
            old_tools = (previous_system or {}).get("toolsAdded", [])
            selected_names = {tool["name"] for tool in declarations}
            removed = [
                {"name": tool["name"]}
                for tool in old_tools
                if tool["name"] not in selected_names
            ]
            if removed:
                system_message["toolsRemoved"] = removed
            session.append_message(system_message)
        # A saved length/error attempt can require overflow compaction even
        # exactly at the threshold. This never continues the previous turn.
        branch = session.branch()
        previous_boundary = max(
            (i for i, entry in enumerate(branch) if entry.get("type") == "compaction"),
            default=-1,
        )
        previous_attempt = next(
            (
                (i, entry)
                for i, entry in reversed(list(enumerate(branch)))
                if entry.get("type") == "message"
                and entry.get("message", {}).get("role") == "assistant"
            ),
            None,
        )
        previous_overflow = False
        if previous_attempt is not None:
            index, entry = previous_attempt
            message = entry["message"]
            if (
                index > previous_boundary
                and message.get("provider") == provider
                and message.get("model") == model
                and compactor.recovery(message)
            ):
                previous_overflow = True
                await compactor.recover(message, entry["id"])
                compactor.overflow_attempted = False
        # Pi checks pre-existing history before submitting a fresh user prompt.
        if not previous_overflow and compactor.threshold():
            await compactor.compact("threshold")
        _event(events, {"type": "agent_start"})
        agent_context = {"messages": session.messages(), "tools": tool_schemas}
        context_overridden = False
        pending_messages = []
        last_completed = None

        def context_append(message):
            agent_context["messages"].append(message)

        def update_runtime(update):
            nonlocal \
                agent_context, \
                context_overridden, \
                provider, \
                model, \
                api, \
                tool_schemas
            if not update:
                return
            if update.get("context") is not None:
                agent_context = update["context"]
                context_overridden = True
                tool_schemas = agent_context.get("tools", tool_schemas)
            target = update.get("model")
            if target is not None:
                if isinstance(target, str):
                    config["llm"]["model_name"] = target
                else:
                    config["llm"]["model_name"] = f"{target['provider']}/{target['id']}"
                    if target.get("api"):
                        config["llm"]["model_api"] = target["api"]
                    if target.get("baseUrl"):
                        config["llm"]["base_url"] = target["baseUrl"]
                provider, model = _model(config)
                api = _api_name(provider, config)
            if "thinkingLevel" in update:
                config["thinking"] = update["thinkingLevel"]
            if _state is not None:
                _state.update(provider=provider, model=model, api=api)

        def turn_context(message, tool_results):
            return {
                "message": message,
                "toolResults": tool_results,
                "context": agent_context,
                "newMessages": generated_messages,
            }

        async def schedule_next(has_tools, decision):
            nonlocal pending_messages
            await _flush(events)
            if decision and decision.get("action") == "end":
                return False
            pending_messages = await drain_queue(
                steering_queue, hooks, "get_steering_messages"
            )
            if has_tools or pending_messages:
                return True
            pending_messages = await drain_queue(
                follow_up_queue, hooks, "get_follow_up_messages"
            )
            return bool(pending_messages) or bool(
                decision and decision.get("action") == "continue"
            )

        _turn = 0
        while max_turns is None or _turn < max_turns:
            if cancel_event is not None and cancel_event.is_set():
                raise asyncio.CancelledError
            _turn += 1
            if user_emitted and compactor.threshold():
                if await compactor.compact("threshold") and not context_overridden:
                    agent_context["messages"] = session.messages()
            if last_completed is not None:
                prepared = await call_hook(
                    hook(hooks, "prepare_next_turn"), last_completed, cancel_event
                )
                update_runtime(prepared)
                prepared_messages = (prepared or {}).get("messages", [])
                if not pending_messages:
                    pending_messages = await drain_queue(
                        steering_queue, hooks, "get_steering_messages"
                    )
            else:
                prepared_messages = []
            _event(events, {"type": "turn_start"})
            if not user_emitted:
                _event(events, {"type": "message_start", "message": user_message})
                _event(events, {"type": "message_end", "message": user_message})
                generated_messages.append(user_message)
                session.append_message(user_message)
                context_append(user_message)
                user_emitted = True
                if _state is not None:
                    _state["user_emitted"] = True
            if _turn == 1 and compactor.threshold():
                if await compactor.compact("threshold") and not context_overridden:
                    agent_context["messages"] = session.messages()
            await _flush(events)
            if _turn == 1:
                pending_messages = await drain_queue(
                    steering_queue, hooks, "get_steering_messages"
                )
            for pending in declare_tool_changes(
                agent_context, [*prepared_messages, *pending_messages], _now_ms()
            ):
                if isinstance(pending, str):
                    pending = {
                        "role": "user",
                        "content": pending,
                        "timestamp": _now_ms(),
                    }
                _event(events, {"type": "message_start", "message": pending})
                _event(events, {"type": "message_end", "message": pending})
                session.append_message(pending)
                generated_messages.append(pending)
                context_append(pending)
            pending_messages = []
            await _flush(events)
            update_runtime(
                await call_hook(
                    hook(hooks, "prepare_request"),
                    {
                        "context": agent_context,
                        "model": {
                            **model_metadata(config),
                            "id": model,
                            "provider": provider,
                            "api": api,
                        },
                        "thinkingLevel": _reasoning_effort(config, config["llm"])
                        or "off",
                    },
                    cancel_event,
                )
            )
            tool_schemas = agent_context.get("tools", tool_schemas)
            request_messages = agent_context["messages"]
            transformed = await call_hook(
                hook(hooks, "transform_context"), request_messages, cancel_event
            )
            if transformed is not None:
                request_messages = transformed
            converted = await call_hook(hook(hooks, "convert_to_llm"), request_messages)
            request_messages = (
                convert_to_llm(request_messages) if converted is None else converted
            )
            api_key = await call_hook(hook(hooks, "get_api_key"), provider)
            if api_key:
                config["llm"]["api_key"] = api_key
            messages = _transport_messages(request_messages)
            streamed = False
            stream_timestamp = _now_ms()

            async def provider_event(event):
                nonlocal streamed
                streamed = True
                event = copy.deepcopy(event)
                kind = event["type"]
                message = (
                    event.get("partial") or event.get("message") or event.get("error")
                )
                if not isinstance(message, dict):
                    return
                message["timestamp"] = stream_timestamp
                if kind == "start":
                    _event(events, {"type": "message_start", "message": message})
                elif kind in {"done", "error"}:
                    message["thinkingLevel"] = (
                        _reasoning_effort(config, config["llm"]) or "off"
                    )
                    _event(events, {"type": "message_end", "message": message})
                else:
                    _event(
                        events,
                        {
                            "type": "message_update",
                            "message": message,
                            "assistantMessageEvent": event,
                        },
                    )
                await _flush(events)

            try:
                reply = await _provider_call(
                    client,
                    config,
                    provider,
                    model,
                    messages,
                    tool_schemas,
                    context_messages=request_messages,
                    on_event=provider_event,
                    cancel_event=cancel_event,
                )
            except Exception as exc:
                # Pi's stream contract represents transport/model failures as a
                # final assistant message with ``stopReason: error``.  Keep the
                # lifecycle complete so consumers can always wait for
                # ``turn_end`` before ``agent_end``/``agent_settled``.
                error_message = {
                    "role": "assistant",
                    "content": [],
                    "api": api,
                    "provider": provider,
                    "model": model,
                    "usage": _pi_usage({}),
                    "stopReason": "error",
                    "thinkingLevel": _reasoning_effort(config, config["llm"]) or "off",
                    "errorMessage": str(exc),
                    "timestamp": _now_ms(),
                }
                _event(
                    events,
                    {
                        "type": "message_start",
                        "message": error_message,
                    },
                )
                _event(events, {"type": "message_end", "message": error_message})
                generated_messages.append(error_message)
                error_entry = session.append_message(error_message)
                context_append(error_message)
                last_completed = turn_context(error_message, [])
                await _flush(events)
                await call_hook(
                    hook(hooks, "finish_turn"), last_completed, cancel_event
                )
                _event(
                    events,
                    {
                        "type": "turn_end",
                        "message": error_message,
                        "toolResults": [],
                    },
                )
                _event(
                    events,
                    {
                        "type": "agent_end",
                        "messages": generated_messages,
                        "willRetry": will_retry(error_message)
                        or (
                            compactor.recovery(error_message)
                            and not compactor.overflow_attempted
                        ),
                    },
                )
                if await retry_turn(error_message, error_entry["id"]):
                    agent_context["messages"] = session.messages()
                    _event(events, {"type": "agent_start"})
                    generated_messages = []
                    if _state is not None:
                        _state["generated_messages"] = generated_messages
                    continue
                if await compactor.recover(error_message, error_entry["id"]):
                    agent_context["messages"] = session.messages()
                    _event(events, {"type": "agent_start"})
                    generated_messages = []
                    if _state is not None:
                        _state["generated_messages"] = generated_messages
                    continue
                if not compactor.recovery(error_message) and compactor.threshold():
                    await compactor.compact("threshold")
                _event(events, {"type": "agent_settled", "aborted": False})
                return NativePiResult(
                    "error",
                    events=events,
                    usage=usage,
                    stop_reason="error",
                    error_message=str(exc),
                )
            provider_usage = _pi_usage(reply.usage)
            _accumulate_usage(usage, provider_usage)
            assistant = reply.message
            assistant_event_message = _assistant_event_message(
                reply,
                provider=provider,
                model=model,
                api=api,
                timestamp=stream_timestamp if streamed else _now_ms(),
            )
            assistant_event_message["thinkingLevel"] = (
                _reasoning_effort(config, config["llm"]) or "off"
            )
            if assistant_event_message["stopReason"] != "error" and retry_attempt:
                _event(
                    events,
                    {
                        "type": "auto_retry_end",
                        "success": True,
                        "attempt": retry_attempt,
                    },
                )
                retry_attempt = 0
            text = _message_text(assistant)
            if not streamed:
                _event(
                    events,
                    {
                        "type": "message_start",
                        "message": {
                            **assistant_event_message,
                            "content": [],
                            "usage": _pi_usage({}),
                            "stopReason": "pending",
                        },
                    },
                )
                if text:
                    _event(
                        events,
                        {
                            "type": "message_update",
                            "usage": _pi_usage(reply.usage),
                            "message": assistant_event_message,
                            "assistantMessageEvent": {
                                "type": "text_delta",
                                "contentIndex": 0,
                                "delta": text,
                            },
                        },
                    )
                for index, call in enumerate(reply.tool_calls, start=1 if text else 0):
                    try:
                        arguments = json.loads(call.get("arguments", "{}"))
                    except (TypeError, json.JSONDecodeError):
                        arguments = {}
                    _event(
                        events,
                        {
                            "type": "message_update",
                            "usage": _pi_usage(reply.usage),
                            "message": assistant_event_message,
                            "assistantMessageEvent": {
                                "type": "toolcall_start",
                                "contentIndex": index,
                                "id": call.get("id", ""),
                                "toolName": call.get("name", ""),
                            },
                        },
                    )
                    _event(
                        events,
                        {
                            "type": "message_update",
                            "usage": _pi_usage(reply.usage),
                            "message": assistant_event_message,
                            "assistantMessageEvent": {
                                "type": "toolcall_end",
                                "contentIndex": index,
                                "toolCall": {
                                    "type": "toolCall",
                                    "id": call.get("id", ""),
                                    "name": call.get("name", ""),
                                    "arguments": arguments
                                    if isinstance(arguments, dict)
                                    else {},
                                },
                            },
                        },
                    )
                _event(
                    events,
                    {
                        "type": "message_end",
                        "message": assistant_event_message,
                    },
                )
            generated_messages.append(assistant_event_message)
            assistant_entry = session.append_message(assistant_event_message)
            context_append(assistant_event_message)
            reason = assistant_event_message["stopReason"]
            await _flush(events)
            if not reply.tool_calls or reason in {"error", "aborted"}:
                last_completed = turn_context(assistant_event_message, [])
                decision = await call_hook(
                    hook(hooks, "finish_turn"), last_completed, cancel_event
                )
                _event(
                    events,
                    {
                        "type": "turn_end",
                        "message": assistant_event_message,
                        "toolResults": [],
                    },
                )
                if reason not in {"error", "aborted"} and await schedule_next(
                    False, decision
                ):
                    continue
                _event(
                    events,
                    {
                        "type": "agent_end",
                        "messages": generated_messages,
                        "willRetry": will_retry(assistant_event_message)
                        or (
                            compactor.recovery(assistant_event_message)
                            and reason != "stop"
                            and not compactor.overflow_attempted
                        ),
                    },
                )
                if await retry_turn(assistant_event_message, assistant_entry["id"]):
                    agent_context["messages"] = session.messages()
                    _event(events, {"type": "agent_start"})
                    generated_messages = []
                    if _state is not None:
                        _state["generated_messages"] = generated_messages
                    continue
                if await compactor.recover(
                    assistant_event_message, assistant_entry["id"]
                ):
                    agent_context["messages"] = session.messages()
                    _event(events, {"type": "agent_start"})
                    generated_messages = []
                    if _state is not None:
                        _state["generated_messages"] = generated_messages
                    continue
                if (
                    reason != "aborted"
                    and not compactor.recovery(assistant_event_message)
                    and compactor.threshold()
                ):
                    await compactor.compact("threshold")
                _event(
                    events, {"type": "agent_settled", "aborted": reason == "aborted"}
                )
                return NativePiResult(
                    "error" if reason in {"error", "aborted"} else "finished",
                    output=text,
                    events=events,
                    usage=usage,
                    stop_reason=reason,
                    error_message=(
                        assistant_event_message.get("errorMessage")
                        or f"Pi response {reason}"
                    )
                    if reason in {"error", "aborted"}
                    else None,
                )
            if _state is not None:
                _state["in_tool_batch"] = True
            tool_results = await _execute_tool_batch(
                reply.tool_calls,
                selected=[
                    schema.get("function", schema)["name"] for schema in tool_schemas
                ],
                cwd=working_dir,
                events=events,
                usage=usage,
                stop_reason=assistant_event_message["stopReason"],
                mode=tool_execution,
                cancel_event=cancel_event,
                model={
                    **model_metadata(config),
                    "id": model,
                    "provider": provider,
                    "api": api,
                },
                auto_resize_images=config.get("images", {}).get("autoResize", True),
                hooks=hooks,
                assistant_message=assistant_event_message,
                context=agent_context,
            )
            if _state is not None:
                _state["in_tool_batch"] = False
            for tool_result in tool_results:
                generated_messages.append(tool_result)
                session.append_message(tool_result)
                context_append(tool_result)
            last_completed = turn_context(assistant_event_message, tool_results)
            decision = await call_hook(
                hook(hooks, "finish_turn"), last_completed, cancel_event
            )
            _event(
                events,
                {
                    "type": "turn_end",
                    "message": assistant_event_message,
                    "toolResults": tool_results,
                },
            )
            if not await schedule_next(not tool_results.terminate, decision):
                _event(events, {"type": "agent_end", "messages": generated_messages})
                _event(events, {"type": "agent_settled", "aborted": False})
                return NativePiResult(
                    "finished",
                    output=text,
                    events=events,
                    usage=usage,
                    stop_reason=reason,
                )
    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError
    _event(
        events,
        {"type": "agent_end", "messages": generated_messages, "willRetry": False},
    )
    _event(events, {"type": "agent_settled", "aborted": False})
    return NativePiResult(
        "error",
        events=events,
        usage=usage,
        stop_reason="length",
        error_message="Pi reached max_turns",
    )


async def run(
    config: Mapping[str, Any],
    prompt: str,
    working_dir: Path,
    files_dir: Path,
    resources: ResourceBundle | None = None,
    *,
    cancel_compaction: asyncio.Event | None = None,
    cancel_event: asyncio.Event | None = None,
    on_event=None,
    steering_queue=None,
    follow_up_queue=None,
    hooks=None,
) -> NativePiResult:
    """Run with an optional abort signal, retaining the completed trajectory.

    Cancelling the Python task itself still propagates cancellation. Explicit
    signal cancellation returns Pi's aborted assistant/lifecycle, and tools see
    the same signal before their subprocesses or filesystem work settle.
    """
    state: dict[str, Any] = {}
    if cancel_event is None:
        result = await _run(
            config,
            prompt,
            working_dir,
            files_dir,
            resources,
            cancel_compaction=cancel_compaction,
            on_event=on_event,
            steering_queue=steering_queue,
            follow_up_queue=follow_up_queue,
            hooks=hooks,
            _state=state,
        )
        await _flush(result.events)
        result.events = list(result.events)
        return result
    operation = asyncio.create_task(
        _run(
            config,
            prompt,
            working_dir,
            files_dir,
            resources,
            cancel_compaction=cancel_compaction or cancel_event,
            cancel_event=cancel_event,
            on_event=on_event,
            steering_queue=steering_queue,
            follow_up_queue=follow_up_queue,
            hooks=hooks,
            _state=state,
        )
    )
    watch = asyncio.create_task(cancel_event.wait())
    try:
        done, _ = await asyncio.wait(
            (operation, watch), return_when=asyncio.FIRST_COMPLETED
        )
        if operation in done:
            result = await operation
            await _flush(result.events)
            result.events = list(result.events)
            return result
        if not state.get("in_tool_batch"):
            operation.cancel()
        try:
            completed = await operation
        except asyncio.CancelledError:
            completed = None
        if completed is not None:
            await _flush(completed.events)
            completed.events = list(completed.events)
            return completed
        events, usage = state.get("events", []), state.get("usage", {})
        generated = state.get("generated_messages", [])
        if state.get("user_emitted"):
            message = {
                "role": "assistant",
                "content": [],
                "api": state["api"],
                "provider": state["provider"],
                "model": state["model"],
                "usage": _pi_usage({}),
                "stopReason": "aborted",
                "errorMessage": "Request aborted",
                "timestamp": _now_ms(),
            }
            state["session"].append_message(message)
            generated.append(message)
            _event(
                events,
                {
                    "type": "message_start",
                    "message": {**message, "stopReason": "pending"},
                },
            )
            _event(events, {"type": "message_end", "message": message})
            _event(events, {"type": "turn_end", "message": message, "toolResults": []})
        last_retry = next(
            (
                event
                for event in reversed(events)
                if event.get("type") in {"auto_retry_start", "auto_retry_end"}
            ),
            None,
        )
        if last_retry is not None and last_retry["type"] == "auto_retry_start":
            _event(
                events,
                {
                    "type": "auto_retry_end",
                    "success": False,
                    "attempt": last_retry["attempt"],
                    "finalError": "Retry cancelled",
                },
            )
        _event(events, {"type": "agent_end", "messages": generated})
        _event(events, {"type": "agent_settled", "aborted": True})
        await _flush(events)
        return NativePiResult(
            "error",
            events=list(events),
            usage=usage,
            stop_reason="aborted",
            error_message="Request aborted",
        )
    finally:
        watch.cancel()
        if not operation.done():
            cancel_event.set()
            operation.cancel()
        await asyncio.gather(operation, watch, return_exceptions=True)


async def compact(
    config: Mapping[str, Any],
    working_dir: Path,
    files_dir: Path,
    custom_instructions: str | None = None,
    resources: ResourceBundle | None = None,
    *,
    cancel_compaction: asyncio.Event | None = None,
) -> NativePiResult:
    """Manually compact an explicit session without continuing its agent turn."""
    prompt = "/compact" + (" " + custom_instructions if custom_instructions else "")
    return await run(
        config,
        prompt,
        working_dir,
        files_dir,
        resources,
        cancel_compaction=cancel_compaction,
    )


async def navigate_session(
    config: Mapping[str, Any],
    target_id: str,
    working_dir: Path,
    files_dir: Path,
    custom_instructions: str | None = None,
    resources: ResourceBundle | None = None,
    *,
    cancel_compaction: asyncio.Event | None = None,
) -> NativePiResult:
    """Summarize the abandoned branch and select a persisted session tree node."""
    prompt = f"/tree {target_id}" + (
        " " + custom_instructions if custom_instructions else ""
    )
    return await run(
        config,
        prompt,
        working_dir,
        files_dir,
        resources,
        cancel_compaction=cancel_compaction,
    )


__all__ = ["NativePiResult", "run", "compact", "navigate_session"]
