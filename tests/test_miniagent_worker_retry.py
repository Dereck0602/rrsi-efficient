"""Request retries keep Worker state and accounting, without paid API calls."""

import asyncio
import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from openai import APIConnectionError, APIStatusError, APITimeoutError

from flashrsi._progress import progress_session
from flashrsi.agents import MiniAgent
from flashrsi.agents.miniagent.runtime import agent, llm
from flashrsi.agents.miniagent.runtime.trace import TraceWriter

RETRY_CONFIG = {
    "model_name": "offline-model",
    "api_key": "private-retry-key",
    "llm_max_retries": 3,
    "llm_retry_policy": "transient",
}


def api_error(kind):
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    if isinstance(kind, int):
        return APIStatusError(
            "upstream private-retry-key",
            response=httpx.Response(kind, request=request),
            body=None,
        )
    if kind == "timeout":
        return APITimeoutError(request=request)
    if kind == "connection":
        return APIConnectionError(request=request)
    if kind == "read_timeout":
        return httpx.ReadTimeout("read timed out", request=request)
    return RuntimeError("invalid model output")


def completion(content="ok", *, finish_reason="stop"):
    return SimpleNamespace(
        id="known-call",
        usage={"prompt_tokens": 10, "completion_tokens": 2},
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(content=content, tool_calls=None),
            )
        ],
    )


@pytest.fixture(autouse=True)
def reset_runtime():
    yield
    llm.configure({})


@pytest.fixture
def backoffs(monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(llm.asyncio, "sleep", sleep)
    return delays


def run_native(tmp_path, **kwargs):
    return asyncio.run(
        agent.run(
            "answer",
            runs_root=tmp_path,
            max_turns=1,
            llm_config=RETRY_CONFIG,
            **kwargs,
        )
    )


def test_gateway_recovery_reuses_request_and_preserves_unknown_cost(
    monkeypatch,
    tmp_path,
    backoffs,
):
    requests = []

    async def chat(messages, **kwargs):
        requests.append(copy.deepcopy((messages, kwargs)))
        if len(requests) <= 2:
            raise api_error(502 if len(requests) == 1 else 503)
        return completion()

    monkeypatch.setattr(llm, "chat", chat)
    result = run_native(tmp_path)
    assert result["stop_reason"] == "completed"
    assert result["turns"] == 1
    assert len(requests) == 3 and requests[0] == requests[1] == requests[2]
    assert backoffs == [2, 4]
    assert result["usage_complete"] is False
    assert len(result["usage"]) == 1
    assert result["usage"][0]["usage"] == {"prompt_tokens": 10, "completion_tokens": 2}
    errors = result["model_request_errors"]
    assert [e["http_status"] for e in errors] == [502, 503]
    assert [e["retry_delay_seconds"] for e in errors] == [2, 4]
    assert "private-retry-key" not in str(result)
    trace = next(tmp_path.glob("*/trace.jsonl")).read_text()
    assert "private-retry-key" not in trace
    assert trace.count('"kind": "model_request_error"') == 2


@pytest.mark.parametrize(
    "kind", [502, 503, 504, "timeout", "connection", "read_timeout"]
)
def test_transient_failure_stops_after_three_attempts(
    monkeypatch,
    tmp_path,
    backoffs,
    kind,
):
    calls = 0

    async def chat(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise api_error(kind)

    monkeypatch.setattr(llm, "chat", chat)
    result = run_native(tmp_path)
    assert calls == 3 and backoffs == [2, 4]
    assert result["stop_reason"].startswith("llm_error:")
    assert result["usage"] == [] and result["usage_complete"] is False
    assert [e["attempt"] for e in result["model_request_errors"]] == [1, 2, 3]
    assert result["model_request_errors"][-1]["retry_delay_seconds"] is None


@pytest.mark.parametrize("kind", [400, 401, 403, 404, 422, 429, 500, "protocol"])
def test_other_errors_do_not_retry(monkeypatch, tmp_path, backoffs, kind):
    calls = 0

    async def chat(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise api_error(kind)

    monkeypatch.setattr(llm, "chat", chat)
    result = run_native(tmp_path)
    assert calls == 1 and backoffs == []
    assert result["model_request_errors"][0]["retry_delay_seconds"] is None


def test_output_budget_failure_is_not_retried(monkeypatch, tmp_path, backoffs):
    calls = 0

    async def chat(*args, **kwargs):
        nonlocal calls
        calls += 1
        return completion("", finish_reason="length")

    monkeypatch.setattr(llm, "chat", chat)
    result = run_native(tmp_path)
    assert calls == 1 and not backoffs
    assert "output budget exhausted" in result["stop_reason"]
    assert result["usage_complete"] is True
    assert len(result["usage"]) == 1


def test_forced_answer_retries_and_keeps_both_successful_usages(
    monkeypatch,
    tmp_path,
    backoffs,
):
    calls = 0

    async def chat(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return completion("<｜｜DSML｜｜ calls>shell")
        if calls == 2:
            raise api_error(504)
        return completion()

    monkeypatch.setattr(llm, "chat", chat)
    result = run_native(tmp_path)
    assert result["answer"] == "ok" and result["stop_reason"] == "completed"
    assert calls == 3 and backoffs == [2]
    assert [c["role"] for c in result["usage"]] == ["primary", "forced_answer"]
    assert result["usage_complete"] is False
    assert result["model_request_errors"][0]["role"] == "forced_answer"


def test_side_request_recovery_still_marks_usage_incomplete(
    monkeypatch,
    tmp_path,
    backoffs,
    caplog,
):
    calls = 0

    async def chat(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise api_error(503)
        return completion()

    llm.configure(RETRY_CONFIG)
    monkeypatch.setattr(llm, "chat", chat)
    trace = TraceWriter(tmp_path, "test", config={"llm": RETRY_CONFIG})
    ctx = agent.Context(workspace=None, trace=trace)
    try:
        assert asyncio.run(ctx.side_text("summarize", purpose="test")) == "ok"
    finally:
        trace.close()
    assert calls == 2 and backoffs == [2]
    assert ctx.usage_complete is False and len(ctx.model_usage) == 1
    assert ctx.model_request_errors[0]["role"] == "side"
    assert "private-retry-key" not in caplog.text


def test_cancellation_during_backoff_does_not_issue_another_request(
    monkeypatch, tmp_path
):
    calls = 0

    async def scenario():
        sleeping = asyncio.Event()

        async def chat(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise api_error(504)

        async def sleep(delay):
            sleeping.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(llm, "chat", chat)
        monkeypatch.setattr(llm.asyncio, "sleep", sleep)
        task = asyncio.create_task(
            agent.run(
                "answer",
                runs_root=tmp_path,
                max_turns=1,
                llm_config=RETRY_CONFIG,
            )
        )
        await asyncio.wait_for(sleeping.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert calls == 1


@pytest.mark.parametrize("profile", ["deepseek", "vectron"])
def test_experiment_target_retains_single_attempt(
    monkeypatch, tmp_path, backoffs, profile
):
    cfg = yaml.safe_load(
        Path(f"examples/terminal_bench21_rrsi_{profile}.yaml").read_text()
    )
    calls = []

    async def chat(*args, **kwargs):
        calls.append(1)
        raise api_error(504)

    monkeypatch.setattr(llm, "chat", chat)
    for role, expected in ((cfg["agent"], 1), (cfg["algorithm"]["worker"], 3)):
        calls.clear()
        request_config = {**role["llm"], "api_key": "private-retry-key"}
        asyncio.run(
            agent.run(
                "answer", runs_root=tmp_path, max_turns=1, llm_config=request_config
            )
        )
        assert len(calls) == expected


@pytest.mark.parametrize(
    "changes",
    [
        {"llm_retry_policy": "forever"},
        {"llm_max_retries": 0},
        {"llm_max_retries": -1},
        {"llm_max_retries": True},
        {"llm_max_retries": 1.5},
    ],
)
def test_invalid_retry_policy_fails_at_configuration(changes):
    config = {**RETRY_CONFIG, **changes}
    with pytest.raises(ValueError):
        MiniAgent({"llm": config})
    with pytest.raises(ValueError):
        llm.configure(config)


@pytest.mark.parametrize("deadline", [20, 1.5])
def test_real_worker_subprocess_retries_within_one_overall_deadline(
    tmp_path,
    deadline,
    monkeypatch,
):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    monkeypatch.setenv("no_proxy", "127.0.0.1")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            requests.append(
                json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            )
            status = 503
            if deadline == 20:
                status = {1: 200, 2: 503, 3: 504}.get(len(requests), 200)
            data = {"error": {"message": "temporary private-retry-key"}}
            if status == 200:
                data = {
                    "id": f"known-call-{len(requests)}",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "offline-model",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "ok"},
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "total_tokens": 12,
                    },
                }
                if len(requests) == 1:
                    data["choices"][0].update(
                        finish_reason="tool_calls",
                        message={
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "one-file-edit",
                                    "type": "function",
                                    "function": {
                                        "name": "shell",
                                        "arguments": json.dumps(
                                            {"command": "printf x >> retry-marker.txt"}
                                        ),
                                    },
                                }
                            ],
                        },
                    )
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    observed = []
    worker = MiniAgent(
        {
            "timeout": deadline,
            "max_turns": 2,
            "llm": {
                **RETRY_CONFIG,
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            },
        }
    )
    worker._observer = lambda prompt, result: (
        observed.append(result) or result.error_message
    )
    output = StringIO()
    try:
        with progress_session("log", stream=output):
            if deadline == 20:
                assert (
                    asyncio.run(worker.edit(tmp_path, "Write the marker, then say ok."))
                    == "ok"
                )
            else:
                with pytest.raises(RuntimeError, match="timed out"):
                    asyncio.run(worker.ask("Say ok."))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert len(observed) == 1
    result = observed[0]
    assert result.usage["accounting_complete"] is False
    assert "private-retry-key" not in str(result) + output.getvalue()
    if deadline == 20:
        assert len(requests) == 4 and requests[1] == requests[2] == requests[3]
        assert (tmp_path / "retry-marker.txt").read_text() == "x"
        assert len(result.usage["model_calls"]) == 2
        errors = [
            m for m in result.messages if m.get("type") == "MiniAgentModelRequestError"
        ]
        assert len(errors) == 2
        assert "failed_attempts=2 retries=2 status=finished" in output.getvalue()
    else:
        assert len(requests) == 1
        assert any(m.get("kind") == "model_request_error" for m in result.messages)
