"""Wire-level parity checks for the native OpenClaw provider boundary."""

from __future__ import annotations

import asyncio
import json

import httpx

from flashrsi.agents.openclaw.runtime.openclaw_native import providers

TOOLS = [
    {
        "name": "read",
        "description": "Read a file",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
]


def _config(api: str, *, stream: bool = False, base_url: str = "https://proxy.test/v1"):
    return {
        "llm": {
            "model_name": "test-model",
            "api_key": "secret",
            "base_url": base_url,
            "model_api": api,
            "stream": stream,
        }
    }


def test_chat_wire_uses_one_assistant_tool_envelope_and_no_duplicate_v1():
    captured = {}

    async def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]},
        )

    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "write"},
        {
            "role": "assistant",
            "content": "working",
            "tool_calls": [{"id": "c1", "name": "read", "arguments": {"path": "a.txt"}}],
        },
        {"role": "tool", "content": {"ok": True}, "tool_call_id": "c1", "name": "read"},
    ]
    async def call():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await providers.complete(_config("chat_completions"), messages, TOOLS, client=client)
        finally:
            await client.aclose()

    result = asyncio.run(call())
    assert result.text == "done"
    assert captured["url"] == "https://proxy.test/v1/chat/completions"
    body = captured["body"]
    assistant = next(item for item in body["messages"] if item["role"] == "assistant")
    assert assistant["content"] == "working"
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"path":"a.txt"}'
    tool = next(item for item in body["messages"] if item["role"] == "tool")
    assert tool["content"] == '{"ok": true}'


def test_chat_wire_preserves_role_request_options_and_extra_body():
    captured = {}

    async def handler(request: httpx.Request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    async def call():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            config = _config("chat_completions")
            config["llm"].update(
                temperature=0.2,
                max_tokens=321,
                reasoning_effort="high",
                extra_body={"service_tier": "priority"},
            )
            return await providers.complete(config, [], TOOLS, client=client)
        finally:
            await client.aclose()

    asyncio.run(call())
    assert captured["body"]["temperature"] == 0.2
    assert captured["body"]["max_tokens"] == 321
    assert captured["body"]["reasoning_effort"] == "high"
    assert captured["body"]["service_tier"] == "priority"


def test_endpoint_base_url_is_not_appended_twice():
    captured = {}

    async def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        )

    async def call():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            config = _config(
                "chat_completions", base_url="https://proxy.test/v1/chat/completions"
            )
            return await providers.complete(config, [], TOOLS, client=client)
        finally:
            await client.aclose()

    result = asyncio.run(call())
    assert result.text == "ok"
    assert captured["url"] == "https://proxy.test/v1/chat/completions"


def test_top_level_api_mode_is_honored():
    captured = {}

    async def handler(request: httpx.Request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"},
        )

    async def call():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            config = {
                "api_mode": "anthropic-messages",
                "llm": {
                    "model_name": "test-model",
                    "api_key": "secret",
                    "base_url": "https://proxy.test/v1",
                    "stream": False,
                },
            }
            return await providers.complete(config, [], TOOLS, client=client)
        finally:
            await client.aclose()

    result = asyncio.run(call())
    assert result.text == "ok"
    assert captured["body"]["model"] == "test-model"


def test_anthropic_wire_groups_tool_results_and_separates_system():
    captured = {}

    async def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"})

    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "do both"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "name": "read", "arguments": {"path": "a"}},
            {"id": "b", "name": "read", "arguments": {"path": "b"}},
        ]},
        {"role": "tool", "content": {"ok": True}, "tool_call_id": "a"},
        {"role": "tool", "content": {"ok": True}, "tool_call_id": "b"},
    ]

    async def call():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await providers.complete(_config("anthropic_messages"), messages, TOOLS, client=client)
        finally:
            await client.aclose()

    asyncio.run(call())
    assert captured["url"] == "https://proxy.test/v1/messages"
    assert captured["body"]["system"] == "rules"
    tool_result_messages = [m for m in captured["body"]["messages"] if m["role"] == "user" and isinstance(m["content"], list) and m["content"][0].get("type") == "tool_result"]
    assert len(tool_result_messages) == 1
    assert [block["tool_use_id"] for block in tool_result_messages[0]["content"]] == ["a", "b"]


def test_responses_wire_uses_top_level_tools_and_function_call_output():
    captured = {}

    async def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "completed", "output_text": "ok", "output": []})

    messages = [
        {"role": "user", "content": "read"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "read", "arguments": {"path": "a"}}]},
        {"role": "tool", "content": "result", "tool_call_id": "c1"},
    ]

    async def call():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await providers.complete(_config("responses"), messages, TOOLS, client=client)
        finally:
            await client.aclose()

    asyncio.run(call())
    assert captured["url"] == "https://proxy.test/v1/responses"
    assert captured["body"]["tools"] == [{"type": "function", "name": "read", "description": "Read a file", "parameters": TOOLS[0]["parameters"]}]
    assert {item["type"] for item in captured["body"]["input"] if "type" in item} == {"function_call", "function_call_output"}


def test_chat_sse_stream_reassembles_text_and_tool_arguments():
    events = []
    first_tool_chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "function": {"name": "read", "arguments": '{"path":'},
                        }
                    ]
                }
            }
        ]
    }
    second_tool_chunk = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [{"index": 0, "function": {"arguments": '"a"}'}}]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    stream = (
        'data: ' + json.dumps({"choices": [{"delta": {"role": "assistant", "content": "hi"}}]}) + '\n\n'
        'data: ' + json.dumps(first_tool_chunk) + '\n\n'
        'data: ' + json.dumps(second_tool_chunk) + '\n\n'
        'data: [DONE]\n\n'
    ).encode()

    async def handler(_request: httpx.Request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    async def on_event(event):
        events.append(event)

    async def call():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await providers.complete(_config("chat_completions", stream=True), [], TOOLS, client=client, event_cb=on_event)
        finally:
            await client.aclose()

    result = asyncio.run(call())
    assert result.text == "hi"
    assert result.tool_calls[0]["id"] == "c1"
    assert result.tool_calls[0]["arguments"] == {"path": "a"}
    assert len(events) == 3


def test_responses_sse_handles_output_item_and_arguments_done():
    stream = b"\n\n".join(
        [
            b'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"fc1","call_id":"call1","name":"read","arguments":""}}',
            b'data: {"type":"response.function_call_arguments.delta","item_id":"fc1","delta":"{\\"path\\":\\"a\\"}"}',
            b'data: {"type":"response.function_call_arguments.done","item_id":"fc1","name":"read","arguments":"{\\"path\\":\\"a\\"}"}',
            b'data: {"type":"response.completed","response":{"status":"completed","usage":{}}}',
            b"data: [DONE]",
        ]
    ) + b"\n\n"

    async def handler(_request: httpx.Request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    async def call():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await providers.complete(_config("responses", stream=True), [], TOOLS, client=client)
        finally:
            await client.aclose()

    result = asyncio.run(call())
    assert result.tool_calls == ({"id": "call1", "name": "read", "arguments": {"path": "a"}},)


def test_responses_sse_does_not_duplicate_call_in_completed_output():
    stream = b"\n\n".join(
        [
            b'data: {"type":"response.output_item.added","item":{"type":"function_call","id":"fc1","call_id":"call1","name":"read","arguments":""}}',
            b'data: {"type":"response.function_call_arguments.delta","item_id":"fc1","delta":"{\\"path\\":\\"a\\"}"}',
            b'data: {"type":"response.completed","response":{"status":"completed","output":[{"type":"function_call","id":"fc1","call_id":"call1","name":"read","arguments":"{\\"path\\":\\"a\\"}"}]}}',
            b"data: [DONE]",
        ]
    ) + b"\n\n"

    async def handler(_request: httpx.Request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=stream)

    async def call():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await providers.complete(_config("responses", stream=True), [], TOOLS, client=client)
        finally:
            await client.aclose()

    result = asyncio.run(call())
    assert result.tool_calls == ({"id": "call1", "name": "read", "arguments": {"path": "a"}},)

def test_chat_wire_separates_tool_text_and_media_attachments():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "name": "read", "arguments": {"path": "pixel.png"}}
            ],
        },
        {
            "role": "tool",
            "content": "Image read successfully",
            "tool_call_id": "c1",
            "name": "read",
            "receipt": {
                "ok": True,
                "attachments": [
                    {"mime": "image/png", "url": "data:image/png;base64,AAAA"}
                ],
            },
        },
    ]

    wire = providers._chat_messages(messages)

    assert wire[1] == {
        "role": "tool",
        "content": "Image read successfully",
        "tool_call_id": "c1",
    }
    media = wire[2]
    assert media["role"] == "user"
    assert media["content"][1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,AAAA"},
    }
    assert "receipt" not in str(wire)

def test_anthropic_and_responses_wire_preserve_media_types():
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "name": "read", "arguments": {}}],
        },
        {
            "role": "tool",
            "content": "Read complete",
            "tool_call_id": "c1",
            "attachments": [
                {"mime": "image/png", "url": "data:image/png;base64,AAAA"},
                {
                    "mime": "application/pdf",
                    "url": "data:application/pdf;base64,JVBERi0=",
                },
            ],
        },
    ]

    _system, anthropic = providers._anthropic_messages(messages)
    block = anthropic[1]["content"][0]
    assert block["type"] == "tool_result"
    assert [part["type"] for part in block["content"]] == ["text", "image", "document"]
    responses = providers._responses_input(messages)
    assert responses[1]["type"] == "function_call_output"
    assert responses[2]["content"][1]["type"] == "input_image"
    assert responses[2]["content"][2]["type"] == "input_file"
