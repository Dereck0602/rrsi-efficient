"""Offline contracts for the pure-Python OpenClaw embedded core."""

from __future__ import annotations

import asyncio

from flashrsi.agents.openclaw.runtime.openclaw_native import (
    compaction,
    loop,
    models,
    policy,
    resources,
    session,
    tools,
)


def test_prompt_skills_and_policy_are_workspace_scoped(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Instructions\n", encoding="utf-8")
    (tmp_path / "TOOLS.md").write_text("tool rules\n", encoding="utf-8")
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\ndescription: demo skill\n---\nUse it.\n", encoding="utf-8"
    )
    loaded = resources.load(tmp_path, tmp_path, {})
    assert "# Instructions" in loaded.system_prompt
    assert "tool rules" in loaded.system_prompt
    assert "demo skill" in loaded.system_prompt
    assert loaded.skills[0].name == "demo"
    checker = policy.ToolPolicy({"tools": {"profile": "coding"}}, tmp_path)
    assert checker.path("x.txt").parent == tmp_path


def test_skill_gating_and_precedence_are_deterministic(tmp_path, monkeypatch):
    workspace_skill = tmp_path / "skills" / "same"
    workspace_skill.mkdir(parents=True)
    (workspace_skill / "SKILL.md").write_text(
        "---\ndescription: workspace\n---\n", encoding="utf-8"
    )
    bundled_skill = tmp_path / "bundled" / "skills" / "same"
    bundled_skill.mkdir(parents=True)
    (bundled_skill / "SKILL.md").write_text(
        "---\ndescription: bundled\n---\n", encoding="utf-8"
    )
    disabled = tmp_path / "skills" / "hidden"
    disabled.mkdir(parents=True)
    (disabled / "SKILL.md").write_text(
        "---\nmetadata:\n  openclaw:\n    disableModelInvocation: true\n---\n", encoding="utf-8"
    )
    loaded = resources.load(tmp_path / "bundled", tmp_path, {})
    assert [skill.name for skill in loaded.skills] == ["same"]
    assert loaded.skills[0].description == "workspace"
    disabled_catalog = resources.load(
        tmp_path / "bundled",
        tmp_path,
        {"prompt": {"skills_mode": "disabled"}},
    )
    assert disabled_catalog.skills == ()
    required = tmp_path / "skills" / "requires-bin"
    required.mkdir()
    (required / "SKILL.md").write_text(
        "---\nmetadata:\n  openclaw:\n    always: true\n    requires:\n      bins: [missing-openclaw-binary]\n---\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(resources, "_has_bin", lambda name: False)
    assert "requires-bin" not in {skill.name for skill in resources.load(tmp_path, tmp_path, {}).skills}


def test_skill_root_and_daily_memory_symlinks_cannot_escape_workspace(tmp_path):
    outside = tmp_path.parent / "openclaw-outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("outside skill", encoding="utf-8")
    (outside / "secret.md").write_text("outside memory", encoding="utf-8")
    (tmp_path / "skills").symlink_to(outside, target_is_directory=True)
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "linked.md").symlink_to(outside / "secret.md")

    loaded = resources.load(tmp_path, tmp_path, {})
    assert loaded.skills == ()
    runner = tools.ToolRunner(tmp_path, tmp_path, policy.ToolPolicy({}, tmp_path))

    async def call():
        return await runner.execute("memory_search", {"query": "outside"})

    assert asyncio.run(call())["results"] == []
    (tmp_path / "skills").unlink()
    (tmp_path / "memory" / "linked.md").unlink()
    (tmp_path / "memory").rmdir()
    (outside / "SKILL.md").unlink()
    (outside / "secret.md").unlink()
    outside.rmdir()


def test_snapshot_defaults_are_loaded_without_overriding_role_config(tmp_path):
    (tmp_path / "config.yaml").write_text(
        "tools:\n  profile: minimal\nprompt:\n  bootstrap_files: [TOOLS.md]\n",
        encoding="utf-8",
    )
    (tmp_path / "TOOLS.md").write_text("snapshot rules", encoding="utf-8")
    defaults = resources.snapshot_config(tmp_path)
    assert defaults["tools"]["profile"] == "minimal"
    loaded = resources.load(tmp_path, tmp_path, defaults)
    assert loaded.system_prompt == "snapshot rules"


def test_bootstrap_file_config_cannot_escape_snapshot_or_workspace(tmp_path):
    outside = tmp_path.parent / "openclaw-secret.md"
    outside.write_text("secret", encoding="utf-8")
    loaded = resources.load(
        tmp_path,
        tmp_path,
        {"prompt": {"bootstrap_files": ["../openclaw-secret.md", str(outside)]}},
    )
    assert "secret" not in loaded.system_prompt
    outside.unlink()


def test_tools_edit_memory_and_boundary(tmp_path):
    runner = tools.ToolRunner(
        tmp_path,
        tmp_path,
        policy.ToolPolicy({"sandbox": {"enabled": True}}, tmp_path),
    )

    async def call():
        assert (await runner.execute("write", {"path": "a.txt", "content": "hello"}))[
            "ok"
        ]
        first_line = await runner.execute("read", {"path": "a.txt", "offset": 1})
        assert first_line["content"] == "hello" and first_line["line_start"] == 1
        assert (
            await runner.execute(
                "edit", {"path": "a.txt", "oldText": "hello", "newText": "world"}
            )
        )["ok"]
        (tmp_path / "MEMORY.md").write_text("remember world", encoding="utf-8")
        result = await runner.execute("memory_search", {"query": "world"})
        assert result["results"]
        try:
            await runner.execute("read", {"path": "../outside.txt"})
        except PermissionError:
            pass
        else:
            raise AssertionError("workspace boundary was not enforced")

    asyncio.run(call())


def test_memory_settings_control_curated_and_daily_files(tmp_path):
    (tmp_path / "MEMORY.md").write_text("curated hit", encoding="utf-8")
    (tmp_path / "USER.md").write_text("user hit", encoding="utf-8")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "2026-11-01.md").write_text("daily hit", encoding="utf-8")

    async def call(config):
        runner = tools.ToolRunner(tmp_path, tmp_path, policy.ToolPolicy(config, tmp_path))
        return await runner.execute("memory_search", {"query": "hit"})

    configured = asyncio.run(
        call({"memory": {"curated_files": ["USER.md"], "daily_dir": "memory"}})
    )
    assert {item["path"] for item in configured["results"]} == {
        "USER.md",
        "memory/2026-11-01.md",
    }
    disabled = asyncio.run(call({"memory": {"enabled": False}}))
    assert disabled["total"] == 0


def test_loop_executes_tool_then_returns_and_persists(tmp_path, monkeypatch):
    calls = []

    async def fake_complete(config, messages, tool_schemas, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return models.ModelResult(
                tool_calls=(
                    {
                        "id": "c1",
                        "name": "write",
                        "arguments": {"path": "answer.txt", "content": "ok"},
                    },
                ),
                usage={"input_tokens": 3, "output_tokens": 2},
            )
        return models.ModelResult(
            text="done",
            stop_reason="stop",
            usage={"input_tokens": 4, "output_tokens": 1},
        )

    monkeypatch.setattr(loop.providers, "complete", fake_complete)
    result = asyncio.run(
        loop.run(
            {"llm": {"model_name": "test", "api_key": "x"}},
            "write it",
            tmp_path,
            tmp_path,
        )
    )
    assert result["status"] == "finished"
    assert result["output"] == "done"
    assert (tmp_path / "answer.txt").read_text(encoding="utf-8") == "ok"
    assert any(event["type"] == "tool_end" for event in result["events"])
    assert (tmp_path / ".openclaw" / "session.sqlite").is_file()


def test_loop_resume_keeps_one_assistant_tool_envelope_and_every_user_turn(
    tmp_path, monkeypatch
):
    calls = []

    async def fake_complete(config, messages, tool_schemas, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return models.ModelResult(
                text="working",
                tool_calls=(
                    {
                        "id": "c1",
                        "name": "write",
                        "arguments": {"path": "answer.txt", "content": "ok"},
                    },
                ),
            )
        return models.ModelResult(text="done", stop_reason="stop")

    monkeypatch.setattr(loop.providers, "complete", fake_complete)
    config = {
        "llm": {"model_name": "test", "api_key": "x"},
        "session_file": str(tmp_path / "session.sqlite"),
        "max_iterations": 3,
    }
    first = asyncio.run(loop.run(config, "first", tmp_path, tmp_path))
    second = asyncio.run(loop.run(config, "second", tmp_path, tmp_path))
    assert first["status"] == second["status"] == "finished"
    # The continuation request sees one assistant message carrying its calls,
    # followed by the matching tool result.  There is no duplicate empty
    # assistant envelope.
    continuation = calls[1]
    assistant = [m for m in continuation if m.get("role") == "assistant"]
    assert assistant[0]["content"] == "working"
    assert assistant[0]["tool_calls"] == [
        {"id": "c1", "name": "write", "arguments": {"path": "answer.txt", "content": "ok"}}
    ]
    assert [m["role"] for m in continuation[-2:]] == ["tool", "assistant"]
    transcript = session.Session(tmp_path / "session.sqlite").messages()
    assert [m["role"] for m in transcript] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
    ]


def test_session_and_compaction_are_deterministic(tmp_path):
    store = session.Session(tmp_path / "s.sqlite")
    store.append("user", "one")
    store.append("assistant", "two")
    assert [m["role"] for m in store.messages()] == ["user", "assistant"]
    messages = [{"role": "system", "content": "system"}] + [
        {"role": "user", "content": "x" * 100} for _ in range(8)
    ]
    compacted = compaction.compact(messages, max_tokens=20, keep_recent=2)
    assert compacted[0]["role"] == "system"
    assert "compacted" in compacted[1]["content"]


def test_compaction_never_leaves_a_dangling_tool_exchange():
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old"},
        {
            "role": "assistant",
            "content": "call",
            "tool_calls": [{"id": "c1", "name": "read", "arguments": {"path": "a"}}],
        },
        {"role": "tool", "content": {"ok": True}, "tool_call_id": "c1"},
        {"role": "user", "content": "recent " * 20},
        {"role": "assistant", "content": "answer"},
    ]
    compacted = compaction.compact(messages, max_tokens=12, keep_recent=4)
    for index, item in enumerate(compacted):
        if item.get("role") == "tool":
            assert index > 0
            assert compacted[index - 1].get("role") == "assistant"
            assert compacted[index - 1].get("tool_calls")


def test_process_tool_lifecycle_and_native_sandbox_default(tmp_path):
    # OpenClaw's sandbox mode is opt-in; the benchmark can still turn it on
    # explicitly (as the boundary test above does).
    checker = policy.ToolPolicy({"tools": {"profile": "full"}}, tmp_path)
    assert checker.sandbox is False
    assert policy.ToolPolicy({"sandbox": {"mode": "workspace"}}, tmp_path).sandbox is True
    assert policy.ToolPolicy({"sandbox": {"mode": "off"}}, tmp_path).sandbox is False
    runner = tools.ToolRunner(tmp_path, tmp_path, checker)

    async def call():
        started = await runner.execute(
            "process", {"action": "run", "command": "printf process-ok"}
        )
        assert started["ok"] and started["sessionId"]
        polled = await runner.execute(
            "process", {"action": "poll", "sessionId": started["sessionId"], "timeout": 2}
        )
        assert polled["running"] is False
        assert "process-ok" in polled["output"]

    asyncio.run(call())


def test_loop_can_disable_transcript_persistence(tmp_path, monkeypatch):
    async def fake_complete(config, messages, tool_schemas, **kwargs):
        return models.ModelResult(text="ok", stop_reason="stop")

    monkeypatch.setattr(loop.providers, "complete", fake_complete)
    result = asyncio.run(
        loop.run(
            {
                "session": {"persist": False},
                "llm": {"model_name": "test", "api_key": "x"},
            },
            "hello",
            tmp_path,
            tmp_path,
        )
    )
    assert result["status"] == "finished"
    assert not (tmp_path / ".openclaw" / "session.sqlite").exists()


def test_loop_does_not_execute_truncated_tool_calls(tmp_path, monkeypatch):
    async def fake_complete(config, messages, tool_schemas, **kwargs):
        return models.ModelResult(
            text="partial",
            stop_reason="length",
            tool_calls=(
                {
                    "id": "bad",
                    "name": "write",
                    "arguments": {"path": "should-not-exist", "content": "x"},
                },
            ),
        )

    monkeypatch.setattr(loop.providers, "complete", fake_complete)
    result = asyncio.run(
        loop.run(
            {"llm": {"model_name": "test", "api_key": "x"}},
            "write",
            tmp_path,
            tmp_path,
        )
    )
    assert result["status"] == "error"
    assert not (tmp_path / "should-not-exist").exists()


def test_loop_records_policy_denials_as_typed_tool_receipts(tmp_path, monkeypatch):
    async def fake_complete(config, messages, tool_schemas, **kwargs):
        return models.ModelResult(
            tool_calls=(
                {"id": "denied", "name": "write", "arguments": {"path": "x", "content": "x"}},
            )
        )

    monkeypatch.setattr(loop.providers, "complete", fake_complete)
    result = asyncio.run(
        loop.run(
            {
                "llm": {"model_name": "test", "api_key": "x"},
                "tools": {"allow": ["read"]},
                "max_iterations": 1,
            },
            "write",
            tmp_path,
            tmp_path,
        )
    )
    receipt = next(event for event in result["events"] if event["type"] == "tool_end")["result"]
    assert receipt == {
        "ok": False,
        "tool": "write",
        "error": "tool denied by policy: write",
        "error_type": "permission_denied",
    }


def test_runtime_event_observer_cannot_mutate_retained_trace():
    event = models.RuntimeEvent("tool_start", {"arguments": {"path": "a.txt"}})
    published = event.as_dict()
    published["arguments"]["path"] = "changed"
    assert event.data["arguments"]["path"] == "a.txt"


def test_loop_event_observer_cannot_mutate_retained_trace(tmp_path, monkeypatch):
    async def fake_complete(config, messages, tool_schemas, **kwargs):
        return models.ModelResult(text="ok", stop_reason="stop")

    async def observer(event):
        if event["type"] == "agent_start":
            event["workspace"] = "mutated"

    monkeypatch.setattr(loop.providers, "complete", fake_complete)
    result = asyncio.run(
        loop.run(
            {"llm": {"model_name": "test", "api_key": "x"}},
            "hello",
            tmp_path,
            tmp_path,
            on_event=observer,
        )
    )
    start = next(event for event in result["events"] if event["type"] == "agent_start")
    assert start["workspace"] != "mutated"

def test_read_byte_cap_reports_complete_total_line_count(tmp_path):
    path = tmp_path / "large.txt"
    path.write_text(
        "\n".join(f"line-{index:04d}-" + "x" * 100 for index in range(600)),
        encoding="utf-8",
    )
    runner = tools.ToolRunner(tmp_path, tmp_path, policy.ToolPolicy({}, tmp_path))

    result = asyncio.run(runner.execute("read", {"path": "large.txt"}))

    assert result["ok"]
    assert result["truncated"] is True
    assert result["total_lines"] == 600
    assert result["line_end"] < result["total_lines"]
    assert (
        "of 600" not in result["output"]
    )  # byte-cap footer points to the continuation offset

def test_loop_keeps_media_attachments_in_provider_transcript(tmp_path, monkeypatch):
    calls = []
    (tmp_path / "pixel.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"pixel")

    async def fake_complete(config, messages, tool_schemas, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return models.ModelResult(
                tool_calls=(
                    {"id": "c1", "name": "read", "arguments": {"path": "pixel.png"}},
                )
            )
        return models.ModelResult(text="done", stop_reason="stop")

    monkeypatch.setattr(loop.providers, "complete", fake_complete)
    result = asyncio.run(
        loop.run(
            {"llm": {"model_name": "test", "api_key": "x"}},
            "inspect",
            tmp_path,
            tmp_path,
        )
    )

    assert result["status"] == "finished"
    tool_message = next(item for item in calls[1] if item["role"] == "tool")
    assert tool_message["content"] == "Image read successfully"
    assert tool_message["attachments"][0]["mime"] == "image/png"
    persisted = session.Session(tmp_path / ".openclaw" / "session.sqlite").messages()
    persisted_tool = next(item for item in persisted if item["role"] == "tool")
    assert persisted_tool["content"] == "Image read successfully"
    assert persisted_tool["receipt"]["attachments"][0]["mime"] == "image/png"

def test_compaction_summary_keeps_tool_attachment_metadata():
    rendered = compaction._serialize(
        {
            "role": "tool",
            "content": "Image read successfully",
            "receipt": {
                "ok": True,
                "attachments": [
                    {
                        "mime": "image/png",
                        "filename": "plot.png",
                        "url": "data:image/png;base64,AA==",
                    }
                ],
            },
        }
    )
    assert "[Attached image/png: plot.png]" in rendered


def test_model_tool_output_never_serializes_receipt_fallback(tmp_path):
    rendered = loop._tool_output(
        "custom",
        {"ok": True, "secret": "internal", "nested": {"receipt": True}},
        tmp_path,
    )
    assert rendered == "Tool completed successfully."
    assert "internal" not in rendered
