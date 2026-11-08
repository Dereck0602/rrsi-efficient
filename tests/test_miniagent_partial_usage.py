"""Recover real subprocess traces after timeout/exit, without making API calls."""

import asyncio
import json
import os
from textwrap import dedent

import pytest

from flashrsi._run_summary import _collect_usage
from flashrsi.agents import MiniAgent
from flashrsi.agents.miniagent import _recover
from flashrsi.agents.miniagent.runtime import agent, llm


@pytest.mark.skipif(os.name != "posix", reason="tests SIGTERM cleanup")
@pytest.mark.parametrize("mode", ["timeout", "crash"])
def test_adapter_recovers_flushed_usage_after_stopping_child(tmp_path, mode):
    source = tmp_path / "source"
    (source / "runtime").mkdir(parents=True)
    (source / "components.yaml").write_text("control_flow: ['runtime/**']\n")
    (source / "runtime/__init__.py").write_text("")
    (source / "runtime/agent.py").write_text(
        dedent("""
        import asyncio, json, sys
        from pathlib import Path

        async def run(**request):
            root = Path(request['runs_root']) / 'run_fixture'
            root.mkdir(parents=True)
            with (root / 'trace.jsonl').open('w') as log:
                def event(value):
                    log.write(json.dumps(value) + '\\n')
                    log.flush()
                event({'kind': 'llm_config', 'api_key': request['llm_config']['api_key']})
                event({'kind': 'model_usage', 'turn': 1, 'role': 'primary',
                       'call_id': 'first', 'usage': {'prompt_tokens': 100, 'completion_tokens': 20}})
                event({'kind': 'tool_result', 'content': 'private-test-key'})
                if request['question'] == 'crash':
                    log.write('{"kind":')
                    log.flush()
                    sys.exit(7)
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    # Recovery must happen AFTER this graceful cleanup flush.
                    event({'kind': 'model_usage', 'turn': 2, 'role': 'side',
                           'call_id': 'cleanup', 'usage': {'prompt_tokens': 3, 'completion_tokens': 4}})
                    log.write('{"kind":')
                    log.flush()
                    raise
    """)
    )
    target = MiniAgent(
        {
            "source_dir": str(source),
            "timeout": 1,
            "llm": {"model_name": "unused", "api_key": "private-test-key"},
        }
    )
    result = asyncio.run(target.run(mode))
    assert result.status == "error"
    assert (
        "timed out" if mode == "timeout" else "exited with 7"
    ) in result.error_message
    assert result.usage["accounting_complete"] is False
    assert result.usage["accounting_source"] == "trace.jsonl"
    assert result.usage["unreadable_trace_entries"] == 1
    calls = result.usage["model_calls"]
    assert [row["call_id"] for row in calls] == (
        ["first", "cleanup"] if mode == "timeout" else ["first"]
    )
    assert "private-test-key" not in str(result)
    assert any(m.get("kind") == "tool_result" for m in result.messages)

    root = tmp_path / "summary"
    (root / "trajectories").mkdir(parents=True)
    (root / "trajectories/00001-baseline.json").write_text(
        json.dumps(
            {
                "trajectories": [
                    {"status": result.status, "usage": dict(result.usage)}
                ],
            }
        )
    )
    total = _collect_usage(root)["total"]
    assert total["input_tokens"] == (103 if mode == "timeout" else 100)
    assert total["output_tokens"] == (24 if mode == "timeout" else 20)
    assert total["records_with_incomplete_usage"] == 1
    assert total["usage_entries_missing_tokens"] == 0
    assert total["duplicate_usage_entries"] == 0


def test_no_trace_is_explicitly_unknown_usage(tmp_path):
    result = _recover(tmp_path, "timeout", [])
    assert result.usage["model_calls"] == () or result.usage["model_calls"] == []
    assert result.usage["accounting_complete"] is False
    assert "total_tokens" not in result.usage


@pytest.mark.parametrize("forced", [False, True])
def test_request_failures_mark_native_usage_incomplete(monkeypatch, tmp_path, forced):
    calls = 0

    async def chat(messages, **kwargs):
        nonlocal calls
        calls += 1
        if forced and calls == 1:
            from types import SimpleNamespace

            return SimpleNamespace(
                id="completed-call",
                usage={"prompt_tokens": 10, "completion_tokens": 2},
                choices=[
                    SimpleNamespace(
                        finish_reason="stop",
                        message=SimpleNamespace(
                            content="<｜｜DSML｜｜ calls>shell", tool_calls=None
                        ),
                    )
                ],
            )
        raise TimeoutError("simulated API timeout")

    monkeypatch.setattr(llm, "chat", chat)
    result = asyncio.run(
        agent.run(
            "answer",
            runs_root=tmp_path,
            max_turns=1,
            llm_config={"model_name": "unused", "llm_max_retries": 1},
        )
    )
    assert result["usage_complete"] is False
    assert len(result["usage"]) == int(forced)
    if forced:
        assert result["usage"][0]["usage"] == {
            "prompt_tokens": 10,
            "completion_tokens": 2,
        }
