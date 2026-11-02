"""Pure Python provider transports for the OpenClaw native snapshot.

The upstream runtime has three wire protocols (OpenAI Chat Completions,
OpenAI Responses and Anthropic Messages).  The loop keeps one small
provider-neutral transcript; this module is the boundary that translates that
transcript to each protocol and translates streamed/non-streamed responses
back to :class:`~.models.ModelResult`.
"""

from __future__ import annotations

import copy
import inspect
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from .models import ModelResult, content_text


def _llm(config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = config.get("llm", config)
    return value if isinstance(value, Mapping) else {}


def _headers(config: Mapping[str, Any], *, anthropic: bool = False) -> dict[str, str]:
    llm = _llm(config)
    key = str(llm.get("api_key", ""))
    headers = {"content-type": "application/json"}
    if anthropic:
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    else:
        headers["authorization"] = f"Bearer {key}"
    extra_headers = llm.get("headers", llm.get("extra_headers"))
    if isinstance(extra_headers, Mapping):
        headers.update({str(k): str(v) for k, v in extra_headers.items()})
    return headers


def _base(config: Mapping[str, Any], default: str) -> str:
    return str(_llm(config).get("base_url") or default).rstrip("/")


def _request_options(
    config: Mapping[str, Any],
    llm: Mapping[str, Any],
    *,
    responses: bool = False,
    anthropic: bool = False,
) -> dict[str, Any]:
    """Carry the generic role request options through the wire boundary.

    FlashRSI keeps provider-specific knobs in ``llm`` so an evolved Snapshot
    can select a route without changing the adapter.  ``extra_body`` is
    intentionally applied last, matching the other native harnesses and
    allowing a provider-specific field to override a generic default.
    """

    options: dict[str, Any] = {}
    for key in ("temperature", "top_p"):
        if key in llm:
            options[key] = llm[key]
    if responses:
        if "max_output_tokens" in llm:
            options["max_output_tokens"] = llm["max_output_tokens"]
        elif "max_tokens" in llm:
            options["max_output_tokens"] = llm["max_tokens"]
    elif anthropic:
        if "max_tokens" in llm:
            options["max_tokens"] = llm["max_tokens"]
    else:
        if "max_completion_tokens" in llm:
            options["max_completion_tokens"] = llm["max_completion_tokens"]
        elif "max_tokens" in llm:
            options["max_tokens"] = llm["max_tokens"]
    effort = llm.get("reasoning_effort", llm.get("effort"))
    if effort is not None and not anthropic:
        options["reasoning_effort"] = effort
    extra = llm.get("extra_body", config.get("extra_body"))
    if isinstance(extra, Mapping):
        options.update(dict(extra))
    return options


def _url(base: str, path: str) -> str:
    """Join an endpoint while avoiding ``.../v1/v1/...``."""

    base = base.rstrip("/")
    path = path.lstrip("/")
    # Accept a fully qualified endpoint as ``base_url``.  OpenClaw provider
    # configs commonly use either the API root (``.../v1``) or the endpoint
    # itself when routing through a proxy.
    # A role may provide either an API root (``.../v1``) or the complete
    # endpoint (``.../v1/chat/completions``). Checking the complete normalized
    # path also handles nested routes such as ``chat/completions``.
    if base.endswith("/" + path):
        return base
    if base.endswith("/v1") and path.startswith("v1/"):
        path = path[3:]
    return f"{base}/{path}"


def _json_args(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(
            value if value is not None else {},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return "{}"


def _object_args(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _attachments(original: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Return validated data URL attachments from a local tool receipt."""

    value = original.get("attachments")
    if not isinstance(value, list):
        receipt = original.get("receipt")
        value = receipt.get("attachments") if isinstance(receipt, Mapping) else None
    if not isinstance(value, list):
        return []
    return [
        item
        for item in value
        if isinstance(item, Mapping)
        and str(item.get("url", "")).startswith("data:")
        and "," in str(item.get("url", ""))
    ]


def _data_attachment(attachment: Mapping[str, Any]) -> tuple[str, str, str] | None:
    url = str(attachment.get("url", ""))
    if not url.startswith("data:") or "," not in url:
        return None
    head, data = url.split(",", 1)
    mime = str(
        attachment.get("mime")
        or head[5:].split(";", 1)[0]
        or "application/octet-stream"
    )
    return mime, url, data


def _tool_schema(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Chat Completions tool envelope (``function`` is nested by design)."""

    return [
        {
            "type": "function",
            "function": {
                "name": str(t["name"]),
                "description": str(t.get("description", "")),
                "parameters": t.get("parameters", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]


def _responses_tool_schema(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI Responses tool envelope (function fields are top-level)."""

    return [
        {
            "type": "function",
            "name": str(t["name"]),
            "description": str(t.get("description", "")),
            "parameters": t.get("parameters", {"type": "object", "properties": {}}),
        }
        for t in tools
    ]


def _normal_call(call: Mapping[str, Any]) -> dict[str, Any]:
    fn = call.get("function") if isinstance(call.get("function"), Mapping) else call
    raw = fn.get("arguments", call.get("arguments", {}))
    try:
        args = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (json.JSONDecodeError, TypeError):
        args = {}
    return {
        "id": str(call.get("call_id") or call.get("id") or "call"),
        "name": str(fn.get("name", call.get("name", ""))),
        "arguments": args if isinstance(args, Mapping) else {},
    }


def _parse_chat(data: Mapping[str, Any]) -> ModelResult:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    calls = tuple(_normal_call(call) for call in (msg.get("tool_calls") or []))
    return ModelResult(content_text(msg.get("content", "")), calls, choice.get("finish_reason"), dict(data.get("usage") or {}), dict(data))


def _parse_anthropic(data: Mapping[str, Any]) -> ModelResult:
    parts = data.get("content") or []
    text = "".join(str(part.get("text") or "") for part in parts if isinstance(part, Mapping) and part.get("type") == "text")
    calls = tuple({"id": str(part.get("id", "call")), "name": str(part.get("name", "")), "arguments": part.get("input") or {}} for part in parts if isinstance(part, Mapping) and part.get("type") == "tool_use")
    return ModelResult(text, calls, data.get("stop_reason"), dict(data.get("usage") or {}), dict(data))


def _chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate provider-neutral tool calls to Chat Completions messages."""

    out: list[dict[str, Any]] = []
    pending_media: list[Mapping[str, Any]] = []

    def flush_media() -> None:
        if not pending_media:
            return
        blocks: list[dict[str, Any]] = [
            {"type": "text", "text": "Attached media from tool result:"}
        ]
        for attachment in pending_media:
            parsed = _data_attachment(attachment)
            if parsed is None:
                continue
            mime, url, _data = parsed
            if mime.startswith("image/"):
                blocks.append({"type": "image_url", "image_url": {"url": url}})
            else:
                blocks.append(
                    {
                        "type": "file",
                        "file": {
                            "file_data": url,
                            "filename": str(attachment.get("filename") or "attachment"),
                        },
                    }
                )
        if len(blocks) > 1:
            out.append({"role": "user", "content": blocks})
        pending_media.clear()

    for original in messages:
        role = str(original.get("role", "user"))
        if role != "tool":
            flush_media()
        content = original.get("content", "")
        calls = original.get("tool_calls") or []
        if calls:
            item = {
                "role": role,
                "content": content or None,
                "tool_calls": [
                    {
                        "id": str(call.get("id") or "call"),
                        "type": "function",
                        "function": {
                            "name": str(call.get("name", "")),
                            "arguments": _json_args(call.get("arguments", {})),
                        },
                    }
                    for call in calls
                ],
            }
        elif role == "tool":
            value = (
                content
                if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False)
            )
            item = {
                "role": "tool",
                "content": value,
                "tool_call_id": str(original.get("tool_call_id") or "call"),
            }
        else:
            item = {"role": role, "content": content}
        out.append(item)
        if role == "tool":
            pending_media.extend(_attachments(original))
    flush_media()
    return out


def _anthropic_messages(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Translate transcript into Anthropic ``system`` + block messages."""

    system_parts: list[str] = []
    out: list[dict[str, Any]] = []
    for original in messages:
        item = dict(original)
        role = str(item.get("role", "user"))
        content = item.get("content", "")
        if role == "system":
            system_parts.append(content_text(content))
            continue
        calls = item.get("tool_calls") or []
        if role == "assistant" and calls:
            blocks: list[dict[str, Any]] = []
            if content_text(content):
                blocks.append({"type": "text", "text": content_text(content)})
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": str(call.get("id") or "call"),
                    "name": str(call.get("name", "")),
                    "input": _object_args(call.get("arguments")),
                }
                for call in calls
            )
            out.append({"role": "assistant", "content": blocks})
        elif role == "tool":
            value = (
                content
                if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False)
            )
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": str(item.get("tool_call_id") or "call"),
                "content": [{"type": "text", "text": value}],
            }
            for attachment in _attachments(item):
                parsed = _data_attachment(attachment)
                if parsed is None:
                    continue
                mime, _url, data = parsed
                if mime.startswith("image/"):
                    block["content"].append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": data,
                            },
                        }
                    )
                elif mime == "application/pdf":
                    block["content"].append(
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": mime,
                                "data": data,
                            },
                        }
                    )
            if not _attachments(item):
                block["content"] = value
            # Anthropic requires alternating user/assistant turns.  A model
            # may emit several tool calls in one assistant envelope, so all
            # corresponding results belong to one user content array.
            if (
                out
                and out[-1].get("role") == "user"
                and isinstance(out[-1].get("content"), list)
            ):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        else:
            # Consecutive user messages are also merged to satisfy Anthropic's
            # alternating-role contract (this occurs after a resumed session).
            if role == "user" and out and out[-1].get("role") == "user":
                previous = out[-1].get("content")
                if not isinstance(previous, list):
                    previous = [{"type": "text", "text": content_text(previous)}]
                    out[-1]["content"] = previous
                if isinstance(content, list):
                    previous.extend(content)
                else:
                    previous.append({"type": "text", "text": content_text(content)})
            else:
                out.append({"role": role, "content": content})
    return "\n\n".join(part for part in system_parts if part), out


def _responses_input(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate transcript to Responses input items."""

    out: list[dict[str, Any]] = []
    pending_media: list[Mapping[str, Any]] = []

    def flush_media() -> None:
        if not pending_media:
            return
        media: list[dict[str, Any]] = []
        for attachment in pending_media:
            parsed = _data_attachment(attachment)
            if parsed is None:
                continue
            mime, url, _data = parsed
            if mime.startswith("image/"):
                media.append({"type": "input_image", "image_url": url})
            elif mime == "application/pdf":
                media.append({"type": "input_file", "file_data": url})
        if media:
            out.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Attached media from tool result:",
                        },
                        *media,
                    ],
                }
            )
        pending_media.clear()

    for original in messages:
        item = dict(original)
        role = str(item.get("role", "user"))
        if role != "tool":
            flush_media()
        content = item.get("content", "")
        calls = item.get("tool_calls") or []
        if role == "assistant" and calls:
            text = content_text(content)
            if text:
                out.append({"role": "assistant", "content": text})
            for call in calls:
                out.append(
                    {
                        "type": "function_call",
                        "call_id": str(call.get("id") or "call"),
                        "name": str(call.get("name", "")),
                        "arguments": _json_args(call.get("arguments", {})),
                    }
                )
        elif role == "tool":
            value = (
                content
                if isinstance(content, str)
                else json.dumps(content, ensure_ascii=False)
            )
            out.append(
                {
                    "type": "function_call_output",
                    "call_id": str(item.get("tool_call_id") or "call"),
                    "output": value,
                }
            )
            pending_media.extend(_attachments(item))
        else:
            out.append({"role": role, "content": content})
    flush_media()
    return out


async def _notify(callback, event: Mapping[str, Any]) -> None:
    if callback is None:
        return
    result = callback(copy.deepcopy(dict(event)))
    if inspect.isawaitable(result):
        await result


def _sse_json(line: str) -> Mapping[str, Any] | None:
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, Mapping) else None


async def _stream_chat(response: httpx.Response, event_cb) -> ModelResult:
    text: list[str] = []
    call_state: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    finish = None
    raw_events: list[dict[str, Any]] = []
    async for line in response.aiter_lines():
        data = _sse_json(line)
        if data is None:
            continue
        raw_events.append(dict(data))
        await _notify(event_cb, data)
        choice = (data.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            text.append(str(delta["content"]))
        finish = choice.get("finish_reason") or finish
        usage.update(data.get("usage") or {})
        for part in delta.get("tool_calls") or []:
            index = int(part.get("index", 0))
            state = call_state.setdefault(index, {"id": "call", "name": "", "arguments": ""})
            if part.get("id"):
                state["id"] = part["id"]
            fn = part.get("function") or {}
            state["name"] += str(fn.get("name") or "")
            state["arguments"] += str(fn.get("arguments") or "")
    return ModelResult("".join(text), tuple(_normal_call(state) for state in call_state.values()), finish, usage, {"events": raw_events})


async def _stream_anthropic(response: httpx.Response, event_cb) -> ModelResult:
    text: list[str] = []
    calls: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    usage: dict[str, Any] = {}
    stop = None
    raw_events: list[dict[str, Any]] = []
    async for line in response.aiter_lines():
        data = _sse_json(line)
        if data is None:
            continue
        raw_events.append(dict(data))
        await _notify(event_cb, data)
        typ = data.get("type")
        if typ == "content_block_start":
            block = data.get("content_block") or {}
            if block.get("type") == "tool_use":
                current = {"id": block.get("id", "call"), "name": block.get("name", ""), "arguments": ""}
                calls.append(current)
        elif typ == "content_block_delta":
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta":
                text.append(str(delta.get("text") or ""))
            elif delta.get("type") == "input_json_delta" and current is not None:
                current["arguments"] += str(delta.get("partial_json") or "")
        elif typ == "message_delta":
            stop = (data.get("delta") or {}).get("stop_reason") or stop
            usage.update(data.get("usage") or {})
        elif typ == "message_start":
            usage.update((data.get("message") or {}).get("usage") or {})
    return ModelResult("".join(text), tuple(_normal_call(call) for call in calls), stop, usage, {"events": raw_events})


async def _stream_responses(response: httpx.Response, event_cb) -> ModelResult:
    text: list[str] = []
    calls: dict[str, dict[str, Any]] = {}
    # Responses streams identify one function call with both an item id
    # (used by argument deltas) and a call id (used by the continuation
    # input).  Keep aliases so ``output_item.done`` and the final
    # ``response.completed`` output cannot create a duplicate call.
    call_aliases: dict[str, str] = {}
    usage: dict[str, Any] = {}
    status = None
    raw_events: list[dict[str, Any]] = []
    async for line in response.aiter_lines():
        data = _sse_json(line)
        if data is None:
            continue
        raw_events.append(dict(data))
        await _notify(event_cb, data)
        typ = data.get("type", "")
        if typ == "response.output_text.delta":
            text.append(str(data.get("delta") or ""))
        elif typ in {"response.output_item.added", "response.output_item.done"}:
            item = data.get("item") or {}
            if item.get("type") == "function_call":
                item_id = str(item.get("id") or "")
                call_id = str(item.get("call_id") or "")
                key = call_aliases.get(item_id) or call_aliases.get(call_id) or call_id or item_id or "call"
                if item_id:
                    call_aliases[item_id] = key
                if call_id:
                    call_aliases[call_id] = key
                state = calls.setdefault(key, {
                    "id": item.get("call_id") or item.get("id") or key,
                    "name": item.get("name", ""),
                    "arguments": "",
                })
                if item.get("name"):
                    state["name"] = item["name"]
                if item.get("arguments") is not None:
                    state["arguments"] = item.get("arguments", "")
        elif typ == "response.function_call_arguments.delta":
            item_id = str(data.get("item_id") or "")
            call_id = str(data.get("call_id") or "")
            key = call_aliases.get(item_id) or call_aliases.get(call_id) or call_id or item_id or "call"
            if item_id:
                call_aliases[item_id] = key
            if call_id:
                call_aliases[call_id] = key
            state = calls.setdefault(key, {"id": data.get("call_id", key), "name": data.get("name", ""), "arguments": ""})
            state["arguments"] += str(data.get("delta") or "")
        elif typ == "response.function_call_arguments.done":
            item_id = str(data.get("item_id") or "")
            call_id = str(data.get("call_id") or "")
            key = call_aliases.get(item_id) or call_aliases.get(call_id) or call_id or item_id or "call"
            if item_id:
                call_aliases[item_id] = key
            if call_id:
                call_aliases[call_id] = key
            state = calls.setdefault(key, {"id": data.get("call_id", key), "name": data.get("name", ""), "arguments": ""})
            state["name"] = data.get("name", state.get("name", ""))
            state["arguments"] = data.get("arguments", state.get("arguments", ""))
        elif typ in {"response.completed", "response.done"}:
            response_data = data.get("response") or {}
            status = response_data.get("status") or status
            usage.update(response_data.get("usage") or {})
            for item in response_data.get("output") or []:
                if item.get("type") == "function_call":
                    item_id = str(item.get("id") or "")
                    call_id = str(item.get("call_id") or "")
                    key = call_aliases.get(item_id) or call_aliases.get(call_id) or call_id or item_id or "call"
                    if item_id:
                        call_aliases[item_id] = key
                    if call_id:
                        call_aliases[call_id] = key
                    state = calls.setdefault(key, {"id": call_id or item_id or key, "name": "", "arguments": ""})
                    state["id"] = call_id or state.get("id") or item_id or key
                    state["name"] = item.get("name", state.get("name", ""))
                    state["arguments"] = item.get("arguments", state.get("arguments", ""))
                elif item.get("type") == "message" and not text:
                    text.append(content_text(item.get("content", "")))
    return ModelResult("".join(text), tuple(_normal_call(call) for call in calls.values()), status, usage, {"events": raw_events})


async def complete(config: Mapping[str, Any], messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, event_cb=None, client: httpx.AsyncClient | None = None) -> ModelResult:
    """Call one provider turn, optionally consuming a true SSE stream."""

    llm = _llm(config)
    model = str(llm.get("model_name") or llm.get("model") or "")
    api = str(
        llm.get("model_api")
        or llm.get("api_mode")
        or config.get("api_mode")
        or "chat_completions"
    ).lower().replace("-", "_")
    # The embedded OpenClaw runner consumes provider streams.  A caller can
    # still opt into a single JSON response for deterministic fixtures.
    stream = bool(llm.get("stream", config.get("stream", True)))
    own = client is None
    client = client or httpx.AsyncClient(timeout=float(config.get("request_timeout", 120)))
    try:
        if api in {"anthropic_messages", "anthropic"}:
            system, wire_messages = _anthropic_messages(messages)
            payload: dict[str, Any] = {"model": model, "max_tokens": int(llm.get("max_tokens", 4096)), "messages": wire_messages}
            payload.update(_request_options(config, llm, anthropic=True))
            if system:
                payload["system"] = system
            if tools:
                payload["tools"] = [{"name": str(t["name"]), "description": str(t.get("description", "")), "input_schema": t.get("parameters", {"type": "object", "properties": {}})} for t in tools]
            if stream:
                payload["stream"] = True
                async with client.stream("POST", _url(_base(config, "https://api.anthropic.com"), "v1/messages"), headers=_headers(config, anthropic=True), json=payload) as response:
                    response.raise_for_status()
                    return await _stream_anthropic(response, event_cb)
            response = await client.post(_url(_base(config, "https://api.anthropic.com"), "v1/messages"), headers=_headers(config, anthropic=True), json=payload)
            response.raise_for_status()
            return _parse_anthropic(response.json())
        if api in {"responses", "openai_responses"}:
            payload: dict[str, Any] = {"model": model, "input": _responses_input(messages)}
            if tools:
                payload["tools"] = _responses_tool_schema(tools)
            payload.update(_request_options(config, llm, responses=True))
            if stream:
                payload["stream"] = True
                async with client.stream("POST", _url(_base(config, "https://api.openai.com/v1"), "responses"), headers=_headers(config), json=payload) as response:
                    response.raise_for_status()
                    return await _stream_responses(response, event_cb)
            response = await client.post(_url(_base(config, "https://api.openai.com/v1"), "responses"), headers=_headers(config), json=payload)
            response.raise_for_status()
            data = response.json()
            calls = []
            text = content_text(data.get("output_text") or "")
            for item in data.get("output") or []:
                if item.get("type") == "message" and not text:
                    text = content_text(item.get("content", ""))
                if item.get("type") == "function_call":
                    calls.append(_normal_call(item))
            return ModelResult(text, tuple(calls), data.get("status"), dict(data.get("usage") or {}), dict(data))
        payload = {"model": model, "messages": _chat_messages(messages)}
        if tools:
            payload["tools"] = _tool_schema(tools)
        payload.update(_request_options(config, llm))
        if stream:
            payload["stream"] = True
            async with client.stream("POST", _url(_base(config, "https://api.openai.com/v1"), "chat/completions"), headers=_headers(config), json=payload) as response:
                response.raise_for_status()
                return await _stream_chat(response, event_cb)
        response = await client.post(_url(_base(config, "https://api.openai.com/v1"), "chat/completions"), headers=_headers(config), json=payload)
        response.raise_for_status()
        return _parse_chat(response.json())
    finally:
        if own:
            await client.aclose()


async def stream_text(*args: Any, **kwargs: Any) -> AsyncIterator[str]:
    """Yield aggregate text for compatibility with the old helper."""

    result = await complete(*args, **kwargs)
    if result.text:
        yield result.text
