"""Offline contracts for the pure-Python OpenCode embedded core."""

from __future__ import annotations

import asyncio

from flashrsi.agents.opencode.runtime.opencode_native import (
    compaction,
    loop,
    models,
    policy,
    resources,
    session,
    tools,
)


def test_opencode_resources_and_canonical_tool_registry(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\ndescription: demo\n---\nUse it.\n", encoding="utf-8"
    )
    loaded = resources.load(tmp_path, tmp_path, {})
    assert "# Rules" in loaded.system_prompt
    assert loaded.skills[0].name == "demo"
    names = {item["name"] for item in tools.schemas()}
    assert {
        "read",
        "write",
        "edit",
        "apply_patch",
        "bash",
        "glob",
        "grep",
        "fetch",
        "diagnostics",
        "task",
        "todowrite",
        "skill",
    } <= names


def test_opencode_skill_result_uses_upstream_envelope(tmp_path):
    skill = tmp_path / "skills" / "demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\ndescription: demo\n---\nUse it.\n", encoding="utf-8"
    )
    (skill / "reference.md").write_text("reference", encoding="utf-8")
    runner = tools.ToolRunner(
        tmp_path,
        tmp_path,
        policy.ToolPolicy({"sandbox": {"enabled": True}}, tmp_path),
    )

    result = asyncio.run(runner.execute("skill", {"name": "demo"}))
    assert result["ok"]
    assert '<skill_content name="demo">' in result["output"]
    assert "# Skill: demo" in result["output"]
    assert f"<file>{skill / 'reference.md'}</file>" in result["output"]


def test_opencode_tools_use_upstream_parameter_names_and_workspace_boundary(tmp_path):
    runner = tools.ToolRunner(
        tmp_path,
        tmp_path,
        policy.ToolPolicy({"sandbox": {"enabled": True}}, tmp_path),
    )

    async def call():
        assert (
            await runner.execute("write", {"filePath": "a.txt", "content": "hello"})
        )["ok"]
        read = await runner.execute("read", {"filePath": "a.txt"})
        assert read["content"] == "hello"
        assert (
            await runner.execute("read", {"filePath": "a.txt", "offset": 0, "limit": 0})
        )["content"] == ""
        edited = await runner.execute(
            "edit",
            {"filePath": "a.txt", "oldString": "hello", "newString": "world"},
        )
        assert edited["ok"]
        globbed = await runner.execute("glob", {"pattern": "*.txt"})
        assert globbed["matches"] == ["a.txt"]
        try:
            await runner.execute("read", {"filePath": "../outside.txt"})
        except PermissionError:
            pass
        else:
            raise AssertionError("workspace boundary was not enforced")

    asyncio.run(call())


def test_opencode_read_matches_upstream_display_and_limits(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")
    runner = tools.ToolRunner(
        tmp_path,
        tmp_path,
        policy.ToolPolicy({"sandbox": {"enabled": True}}, tmp_path),
    )

    async def call():
        whole = await runner.execute("read", {"filePath": "a.txt"})
        assert whole["output"].startswith(
            f"<path>{target}</path>\n<type>file</type>\n<content>"
        )
        assert "1: one" in whole["output"]
        assert "2: two" in whole["output"]
        assert "(End of file - total 2 lines)" in whole["output"]
        partial = await runner.execute(
            "read", {"filePath": "a.txt", "offset": 1, "limit": 1}
        )
        assert "1: one" in partial["output"]
        assert "Showing lines 1-1 of 2" in partial["output"]
        long = tmp_path / "long.txt"
        long.write_text("x" * 2100, encoding="utf-8")
        capped = await runner.execute("read", {"filePath": "long.txt"})
        assert "line truncated to 2000 chars" in capped["output"]

    asyncio.run(call())


def test_opencode_read_textdecoder_and_binary_markers_match_upstream(tmp_path):
    invalid = tmp_path / "invalid.txt"
    invalid.write_bytes(b"ok \xff\n")
    nul = tmp_path / "nul.txt"
    nul.write_bytes(b"mostly text\x00\n")
    runner = tools.ToolRunner(
        tmp_path,
        tmp_path,
        policy.ToolPolicy({"sandbox": {"enabled": True}}, tmp_path),
    )

    async def call():
        decoded = await runner.execute("read", {"filePath": "invalid.txt"})
        assert decoded["ok"]
        assert "ok �" in decoded["output"]
        binary = await runner.execute("read", {"filePath": "nul.txt"})
        assert not binary["ok"]
        assert binary["error_type"] == "binary_file"

    asyncio.run(call())


def test_opencode_read_attaches_nested_instruction_once(tmp_path):
    nested = tmp_path / "src"
    nested.mkdir()
    (nested / "AGENTS.md").write_text("Use src conventions.\n", encoding="utf-8")
    (nested / "a.txt").write_text("hello\n", encoding="utf-8")
    runner = tools.ToolRunner(
        tmp_path,
        tmp_path,
        policy.ToolPolicy({"sandbox": {"enabled": True}}, tmp_path),
    )

    async def call():
        first = await runner.execute("read", {"filePath": "src/a.txt"})
        second = await runner.execute("read", {"filePath": "src/a.txt"})
        assert first["metadata"]["loaded"] == [str(nested / "AGENTS.md")]
        assert "<system-reminder>" in first["output"]
        assert second["metadata"]["loaded"] == []
        assert "<system-reminder>" not in second["output"]

    asyncio.run(call())


def test_opencode_apply_patch_and_last_match_permission_rules(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("one\ntwo\n", encoding="utf-8")
    checker = policy.ToolPolicy(
        {"permissions": {"*": "allow", "edit": {"*": "allow", "*.env": "deny"}}},
        tmp_path,
    )
    assert checker.permitted("edit", "a.txt")
    assert not checker.permitted("edit", "secret.env")
    runner = tools.ToolRunner(tmp_path, tmp_path, checker)

    async def call():
        result = await runner.execute(
            "apply_patch",
            {
                "patchText": "*** Begin Patch\n*** Update File: a.txt\n@@\n-one\n+ONE\n*** End Patch"
            },
        )
        assert result["ok"]

    asyncio.run(call())
    assert target.read_text(encoding="utf-8") == "ONE\ntwo\n"


def test_opencode_edit_matches_creation_bom_and_line_ending_rules(tmp_path):
    runner = tools.ToolRunner(
        tmp_path,
        tmp_path,
        policy.ToolPolicy({"sandbox": {"enabled": True}}, tmp_path),
    )

    async def call():
        created = await runner.execute(
            "edit", {"filePath": "new.txt", "oldString": "", "newString": "one"}
        )
        assert created["ok"]
        assert (tmp_path / "new.txt").read_text() == "one"
        (tmp_path / "crlf.txt").write_bytes(b"\xef\xbb\xbfone\r\ntwo\r\n")
        changed = await runner.execute(
            "edit",
            {"filePath": "crlf.txt", "oldString": "one\ntwo", "newString": "ONE\nTWO"},
        )
        assert changed["ok"]
        assert (tmp_path / "crlf.txt").read_bytes() == b"\xef\xbb\xbfONE\r\nTWO\r\n"
        identical = await runner.execute(
            "edit", {"filePath": "crlf.txt", "oldString": "x", "newString": "x"}
        )
        assert identical["error_type"] == "no_changes"

    asyncio.run(call())


def test_opencode_loop_executes_tool_then_returns_and_persists(tmp_path, monkeypatch):
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
                        "arguments": {"filePath": "answer.txt", "content": "ok"},
                    },
                ),
            )
        return models.ModelResult(text="done", stop_reason="stop")

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
    tool_messages = [item for item in calls[1] if item.get("role") == "tool"]
    assert tool_messages[0]["content"] == "Wrote file successfully."
    assert (tmp_path / ".opencode" / "session.sqlite").is_file()
    assert (
        session.Session(tmp_path / ".opencode" / "session.sqlite").messages()[0]["role"]
        == "user"
    )


def test_opencode_model_tool_output_matches_read_envelope(tmp_path):
    path = tmp_path / "a.txt"
    path.write_text("one\ntwo\n", encoding="utf-8")
    runner = tools.ToolRunner(
        tmp_path,
        tmp_path,
        policy.ToolPolicy({"sandbox": {"enabled": True}}, tmp_path),
    )

    async def call():
        return await runner.execute("read", {"filePath": "a.txt"})

    receipt = asyncio.run(call())
    rendered = loop._tool_output("read", receipt, tmp_path)
    assert rendered.startswith(f"<path>{path}</path>\n<type>file</type>")
    assert "1: one" in rendered
    assert "2: two" in rendered
    assert rendered.endswith("(End of file - total 2 lines)\n</content>")


def test_opencode_webfetch_formats_html_without_external_converter():
    html = "<html><head><style>hidden</style></head><body><h1>Title</h1><p>Hello <b>world</b>.</p></body></html>"
    markdown = tools._webfetch_format(html, "text/html", "markdown")
    text = tools._webfetch_format(html, "text/html", "text")
    assert "# Title" in markdown
    assert "Hello world." in markdown
    assert "<h1>" not in markdown
    assert "#" not in text
    assert "hidden" not in text


def test_opencode_glob_and_grep_cap_and_render_results(tmp_path):
    for index in range(105):
        (tmp_path / f"item-{index}.txt").write_text("needle\n", encoding="utf-8")
    runner = tools.ToolRunner(
        tmp_path,
        tmp_path,
        policy.ToolPolicy({"sandbox": {"enabled": True}}, tmp_path),
    )

    async def call():
        return await runner.execute("glob", {"pattern": "*.txt"}), await runner.execute(
            "grep", {"pattern": "needle", "include": "*.txt"}
        )

    globbed, grepped = asyncio.run(call())
    assert globbed["count"] == 100
    assert globbed["truncated"] is True
    assert "Results are truncated" in globbed["output"]
    assert grepped["count"] == 100
    assert grepped["truncated"] is True
    assert "Results truncated" in grepped["output"]


def test_opencode_content_filter_finish_is_reported_as_error(tmp_path, monkeypatch):
    async def fake_complete(config, messages, tool_schemas, **kwargs):
        return models.ModelResult(text="blocked", stop_reason="content_filter")

    monkeypatch.setattr(loop.providers, "complete", fake_complete)
    result = asyncio.run(
        loop.run(
            {"llm": {"model_name": "test", "api_key": "x"}, "max_iterations": 3},
            "unsafe",
            tmp_path,
            tmp_path,
        )
    )
    assert result["status"] == "error"
    assert "content filter" in result["error_message"]
    assert result["events"][-1]["type"] == "agent_end"


def test_opencode_last_step_uses_upstream_prompt_and_disables_tools(
    tmp_path, monkeypatch
):
    seen = {}

    async def fake_complete(config, messages, tool_schemas, **kwargs):
        seen["messages"] = messages
        seen["tools"] = tool_schemas
        seen["choice"] = kwargs.get("tool_choice")
        return models.ModelResult(text="summary", stop_reason="stop")

    monkeypatch.setattr(loop.providers, "complete", fake_complete)
    result = asyncio.run(
        loop.run(
            {"llm": {"model_name": "test", "api_key": "x"}, "max_iterations": 1},
            "finish",
            tmp_path,
            tmp_path,
        )
    )
    assert result["status"] == "finished"
    assert seen["tools"] == []
    assert seen["choice"] == "none"
    assert loop.MAX_STEPS_PROMPT in seen["messages"][-1]["content"]


def test_opencode_length_with_tool_call_keeps_continuation(tmp_path, monkeypatch):
    calls = []

    async def fake_complete(config, messages, tool_schemas, **kwargs):
        calls.append((messages, tool_schemas))
        if len(calls) == 1:
            return models.ModelResult(
                text="partial",
                stop_reason="length",
                tool_calls=(
                    {
                        "id": "c1",
                        "name": "write",
                        "arguments": {"filePath": "x", "content": "ok"},
                    },
                ),
            )
        return models.ModelResult(text="done", stop_reason="stop")

    monkeypatch.setattr(loop.providers, "complete", fake_complete)
    result = asyncio.run(
        loop.run(
            {"llm": {"model_name": "test", "api_key": "x"}, "max_iterations": 3},
            "write",
            tmp_path,
            tmp_path,
        )
    )
    assert result["status"] == "finished"
    assert (tmp_path / "x").read_text(encoding="utf-8") == "ok"


def test_opencode_compaction_bounds_a_single_oversized_turn():
    messages = [{"role": "user", "content": "x" * 10_000}]
    compacted = compaction.compact(messages, max_tokens=100)
    assert compaction.estimate_tokens(compacted) <= 100


def test_opencode_compaction_preserves_recent_tool_envelope_and_serializes_history():
    messages = [
        {"role": "user", "content": "old task"},
        {
            "role": "assistant",
            "content": "old call",
            "tool_calls": [
                {"id": "old", "name": "read", "arguments": {"filePath": "a"}}
            ],
        },
        {
            "role": "tool",
            "name": "read",
            "content": "x" * 12_000,
            "tool_call_id": "old",
        },
        {"role": "user", "content": "new task"},
        {
            "role": "assistant",
            "content": "new call",
            "tool_calls": [
                {"id": "new", "name": "read", "arguments": {"filePath": "b"}}
            ],
        },
        {
            "role": "tool",
            "name": "read",
            "content": "new result",
            "tool_call_id": "new",
        },
    ]
    compacted = compaction.compact(
        messages, max_tokens=500, preserve_recent_tokens=40, tail_turns=1
    )
    assert any(
        "[Assistant tool call]: read" in str(item.get("content")) for item in compacted
    )
    first_non_system = next(item for item in compacted if item.get("role") != "system")
    assert first_non_system["role"] in {"user", "assistant"}


def test_opencode_compaction_prunes_only_old_tool_results():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "tool", "name": "read", "content": "x" * 200, "tool_call_id": "old"},
        {"role": "user", "content": "second"},
        {"role": "tool", "name": "read", "content": "middle", "tool_call_id": "middle"},
        {"role": "user", "content": "third"},
        {"role": "tool", "name": "read", "content": "y" * 200, "tool_call_id": "new"},
    ]
    pruned = compaction.prune_tool_results(
        messages, protect_tokens=10, minimum_tokens=1
    )
    assert pruned[1]["content"] == "[Old tool result content cleared]"
    assert pruned[5]["content"] == "y" * 200


def test_opencode_compaction_prune_keeps_immediately_previous_turn():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "tool", "name": "read", "content": "x" * 400},
        {"role": "user", "content": "second"},
        {"role": "tool", "name": "read", "content": "y" * 400},
        {"role": "user", "content": "third"},
        {"role": "tool", "name": "read", "content": "z" * 400},
    ]
    pruned = compaction.prune_tool_results(messages, protect_tokens=1, minimum_tokens=1)
    assert pruned[1]["content"] == "[Old tool result content cleared]"
    assert pruned[3]["content"] == "y" * 400
    assert pruned[5]["content"] == "z" * 400


def test_opencode_compaction_tail_zero_summarizes_all_history():
    messages = [
        {"role": "user", "content": "old" * 100},
        {"role": "assistant", "content": "answer" * 100},
        {"role": "user", "content": "new" * 100},
    ]
    compacted = compaction.compact(
        messages, max_tokens=250, preserve_recent_tokens=1, tail_turns=0
    )
    assert len(compacted) == 1
    assert compacted[0]["role"] == "assistant"
    assert "[User]: old" in compacted[0]["content"]
    assert all(item["role"] != "user" for item in compacted)


def test_opencode_compaction_summary_keeps_tool_attachment_metadata():
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


def test_opencode_model_tool_output_never_serializes_receipt_fallback(tmp_path):
    rendered = loop._tool_output(
        "custom",
        {"ok": True, "secret": "internal", "nested": {"receipt": True}},
        tmp_path,
    )
    assert rendered == "Tool completed successfully."
    assert "internal" not in rendered


def test_opencode_compaction_oversized_latest_turn_falls_back_to_full_summary():
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": "latest" * 3000},
    ]
    compacted = compaction.compact(
        messages, max_tokens=500, preserve_recent_tokens=10, tail_turns=1
    )
    assert len(compacted) == 1
    assert compacted[0]["role"] == "assistant"
