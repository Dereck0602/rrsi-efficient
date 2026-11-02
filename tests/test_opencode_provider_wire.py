"""Wire contracts for the OpenCode native provider boundary."""

from __future__ import annotations

import asyncio
import json

import httpx

from flashrsi.agents.opencode.runtime.opencode_native import providers

TOOLS = [
    {
        "name": "read",
        "description": "Read a file",
        "parameters": {
            "type": "object",
            "properties": {"filePath": {"type": "string"}},
        },
    }
]


def _config(api: str):
    return {
        "llm": {
            "model_name": "test",
            "api_key": "secret",
            "base_url": "https://proxy.test/v1",
            "model_api": api,
            "stream": False,
        }
    }


def test_chat_wire_uses_opencode_tools_and_continuation_envelope():
    captured = {}

    async def handler(request: httpx.Request):
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}]
            },
        )

    messages = [
        {"role": "user", "content": "read"},
        {
            "role": "assistant",
            "content": "working",
            "tool_calls": [
                {"id": "c1", "name": "read", "arguments": {"filePath": "a.txt"}}
            ],
        },
        {"role": "tool", "content": {"ok": True}, "tool_call_id": "c1", "name": "read"},
    ]

    async def call():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await providers.complete(
                _config("chat_completions"), messages, TOOLS, client=client
            )
        finally:
            await client.aclose()

    result = asyncio.run(call())
    assert result.text == "done"
    assert captured["url"] == "https://proxy.test/v1/chat/completions"
    body = captured["body"]
    assistant = next(item for item in body["messages"] if item["role"] == "assistant")
    assert assistant["tool_calls"][0]["function"]["name"] == "read"
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"filePath":"a.txt"}'
    tool = next(item for item in body["messages"] if item["role"] == "tool")
    assert "name" not in tool


def test_chat_wire_strips_snapshot_session_metadata_from_tool_message():
    wire = providers._chat_messages(
        [
            {
                "role": "tool",
                "content": "Wrote file successfully.",
                "tool_call_id": "c1",
                "name": "write",
                "receipt": {"ok": True, "sha256": "local-only"},
            }
        ]
    )
    assert wire == [
        {
            "role": "tool",
            "content": "Wrote file successfully.",
            "tool_call_id": "c1",
        }
    ]


def test_media_tool_receipts_use_open_code_synthetic_attachment_turns():
    attachment = {
        "type": "file",
        "mime": "image/png",
        "url": "data:image/png;base64,AA==",
    }
    wire = providers._chat_messages(
        [
            {
                "role": "tool",
                "content": "Image read successfully",
                "tool_call_id": "c1",
                "receipt": {"ok": True, "attachments": [attachment]},
            }
        ]
    )
    assert wire[0]["role"] == "tool"
    assert wire[1] == {
        "role": "user",
        "content": [
            {"type": "text", "text": "Attached media from tool result:"},
            {"type": "image_url", "image_url": {"url": attachment["url"]}},
        ],
    }


def test_responses_wire_uses_function_call_output():
    captured = {}

    async def handler(request: httpx.Request):
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"status": "completed", "output_text": "ok", "output": []}
        )

    messages = [
        {"role": "user", "content": "read"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "c1", "name": "read", "arguments": {"filePath": "a"}}
            ],
        },
        {"role": "tool", "content": "result", "tool_call_id": "c1"},
    ]

    async def call():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await providers.complete(
                _config("responses"), messages, TOOLS, client=client
            )
        finally:
            await client.aclose()

    asyncio.run(call())
    assert captured["body"]["tools"] == [
        {
            "type": "function",
            "name": "read",
            "description": "Read a file",
            "parameters": TOOLS[0]["parameters"],
        }
    ]
    assert {item["type"] for item in captured["body"]["input"] if "type" in item} == {
        "function_call",
        "function_call_output",
    }


def test_responses_nonstream_uses_call_id_for_continuation():
    async def handler(_request: httpx.Request):
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_item_1",
                        "call_id": "call_1",
                        "name": "read",
                        "arguments": '{"filePath":"a"}',
                    }
                ],
            },
        )

    async def call():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await providers.complete(
                _config("responses"),
                [{"role": "user", "content": "read"}],
                TOOLS,
                client=client,
            )
        finally:
            await client.aclose()

    result = asyncio.run(call())
    assert result.tool_calls[0]["id"] == "call_1"


def test_chat_sse_reassembles_text_and_tool_arguments():
    first = {"choices": [{"delta": {"content": "hi"}}]}
    second = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "c1",
                            "function": {"name": "read", "arguments": '{"filePath":'},
                        }
                    ]
                }
            }
        ]
    }
    third = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [{"index": 0, "function": {"arguments": '"a.txt"}'}}]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    stream = (
        "data: " + json.dumps(first) + "\n\n"
        "data: " + json.dumps(second) + "\n\n"
        "data: " + json.dumps(third) + "\n\n"
        "data: [DONE]\n\n"
    ).encode()

    async def handler(_request: httpx.Request):
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, content=stream
        )

    async def call():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            return await providers.complete(
                _config("chat_completions")
                | {"llm": {**_config("chat_completions")["llm"], "stream": True}},
                [],
                TOOLS,
                client=client,
            )
        finally:
            await client.aclose()

    result = asyncio.run(call())
    assert result.text == "hi"
    assert result.tool_calls[0]["arguments"] == {"filePath": "a.txt"}
