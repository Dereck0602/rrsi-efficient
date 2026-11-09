"""Native surfaces, false-positive guards, exploration and resume identity."""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from test_rrsi import change, saved, setup

from flashrsi.algorithms.rrsi._checkpoint import CheckpointResume, Checkpoints
from flashrsi.algorithms.rrsi._components import (
    TAGS,
    UNKNOWN,
    available_components,
    classify_edit,
)
from flashrsi.core.snapshot import SnapshotFile

AGENTS = Path(__file__).resolve().parents[1] / "flashrsi/agents"


def pair(agent, path, before, after, component="code"):
    directory = "pi_agent" if agent == "pi" else agent
    manifest = AGENTS / directory / "components.yaml"
    rules = (
        yaml.safe_load(manifest.read_text())
        if manifest.exists()
        else {component: ["**"]}
    )

    def snap(text):
        return SimpleNamespace(
            agent_type=agent,
            component_rules=rules,
            files={path: SnapshotFile(text.encode(), component)} if text else {},
        )

    return snap(before), snap(after)


def classify(agent, path, before, after, declared, component="code", custom=None):
    old, new = pair(agent, path, before, after, component)
    return classify_edit(
        old, new, {"component": declared, "paths": [path]}, custom or {}
    )


@pytest.mark.parametrize(
    "agent,path,component,tag,before,after",
    [
        (
            "miniagent",
            "prompts/system.md",
            "prompt",
            "prompt",
            "Check.",
            "Check twice.",
        ),
        (
            "miniagent",
            "skills/main.md",
            "skill",
            "skill",
            "Check.",
            "Reload artifacts.",
        ),
        ("miniagent", "agent.yaml", "config", "config", "limit: 1\n", "limit: 2\n"),
        (
            "miniagent",
            "runtime/agent.py",
            "code",
            "control_flow",
            "done = False\n",
            "done = True\nif done:\n    stop()\n",
        ),
        (
            "miniagent",
            "runtime/agent.py",
            "code",
            "context_mgmt",
            "messages = history\n",
            "messages = trim_history(history)\n",
        ),
        (
            "miniagent",
            "runtime/trace.py",
            "code",
            "output_plumbing",
            "directory.mkdir()\n",
            "directory.mkdir(exist_ok=True)\n",
        ),
        (
            "miniagent",
            "tools/shell.py",
            "tool",
            "client_tool",
            "elapsed = 0\n",
            "elapsed = clock() - start\n",
        ),
        (
            "miniagent",
            "middleware/notes.py",
            "code",
            "memory",
            "",
            "def after_tool(ctx):\n    ctx.memory.remember(ctx.result)\n",
        ),
        (
            "miniagent",
            "sub_agents/reviewer.py",
            "code",
            "subagent",
            "",
            "def run(ctx):\n    return ctx.side_text(ctx.question)\n",
        ),
        (
            "codex",
            "python_core/assets/prompt.md",
            "prompt",
            "prompt",
            "Check.",
            "Check twice.",
        ),
        (
            "codex",
            "skills/check/SKILL.md",
            "skill",
            "skill",
            "",
            "Check the deliverable.",
        ),
        ("codex", "config.toml", "config", "config", "limit=1\n", "limit=2\n"),
        (
            "codex",
            "python_core/loop.py",
            "code",
            "control_flow",
            "limit = 1\n",
            "limit = 2\n",
        ),
        (
            "codex",
            "python_core/compact.py",
            "code",
            "context_mgmt",
            "limit = 1\n",
            "limit = 2\n",
        ),
        (
            "codex",
            "python_core/client.py",
            "code",
            "output_plumbing",
            "limit = 1\n",
            "limit = 2\n",
        ),
        (
            "codex",
            "python_core/shell.py",
            "tool",
            "client_tool",
            "limit = 1\n",
            "limit = 2\n",
        ),
        (
            "codex",
            "python_core/memory.py",
            "memory",
            "memory",
            "limit = 1\n",
            "limit = 2\n",
        ),
        (
            "codex",
            "python_core/subagents.py",
            "agent",
            "subagent",
            "limit = 1\n",
            "limit = 2\n",
        ),
        ("claude_code", "CLAUDE.md", "prompt", "prompt", "Check.", "Check twice."),
        (
            "claude_code",
            "commands/check.md",
            "skill",
            "skill",
            "",
            "Check the deliverable.",
        ),
        (
            "claude_code",
            "settings.json",
            "config",
            "config",
            "{}",
            '{"permissions":{"allow":["Read"]}}',
        ),
        (
            "claude_code",
            "hooks/check.py",
            "hook",
            "control_flow",
            "",
            "if failed:\n    raise SystemExit(2)\n",
        ),
        (
            "claude_code",
            "hooks/hooks.json",
            "hook",
            "context_mgmt",
            "{}",
            '{"hooks":{"PreCompact":[{"hooks":[{"type":"command","command":"./compact.sh"}]}]}}',
        ),
        (
            "claude_code",
            "hooks/report.py",
            "hook",
            "output_plumbing",
            "",
            "import json\nprint(json.dumps(output))\n",
        ),
        (
            "claude_code",
            ".mcp.json",
            "tool",
            "client_tool",
            "{}",
            '{"mcpServers":{"local":{"command":"python","args":["server.py"]}}}',
        ),
        (
            "claude_code",
            "agents/reviewer.md",
            "agent",
            "subagent",
            "",
            "---\nname: reviewer\ndescription: Check work\n---\nReview the output.",
        ),
    ],
)
def test_native_surfaces(agent, path, component, tag, before, after):
    result = classify(agent, path, before, after, tag, component)
    assert result["component"] == tag, result
    assert result["profile"] == agent and result["evidence"][tag]


@pytest.mark.parametrize("agent", ["pi", "hermes", "openclaw", "opencode"])
def test_other_embedded_runtimes(agent):
    for name, tag in [
        ("loop", "control_flow"),
        ("compaction", "context_mgmt"),
        ("tools", "client_tool"),
        ("session", "output_plumbing"),
    ]:
        assert (
            classify(
                agent,
                f"runtime/{agent}_native/{name}.py",
                "limit=1\n",
                "limit=2\n",
                tag,
                "runtime",
            )["component"]
            == tag
        )


@pytest.mark.parametrize("tag", ["memory", "client_tool", "subagent", "context_mgmt"])
def test_comments_docstrings_and_unchanged_context_cannot_earn_exploration(tag):
    original = 'def f():\n    """old"""\n    return 1\n'
    revised = 'def f():\n    """Memory() ToolRegistry subagent context"""\n    # remember()\n    return 1\n'
    result = classify(
        "miniagent",
        "runtime/agent.py",
        original,
        revised,
        tag,
        custom={tag: ["Memory", "runtime/"]},
    )
    assert result["component"] == "prompt"
    original = "memory = Memory()\n\ndef f():\n    return 1\n"
    revised = original.replace("return 1", "return 2")
    result = classify("miniagent", "runtime/agent.py", original, revised, "memory")
    assert "memory" not in result["evidence"]


def test_tool_description_is_prompt_but_schema_change_is_tool():
    before = '{"name":"shell","description":"old","parameters":{"type":"object"}}'
    after = before.replace('"old"', '"Memory() subagent tool_spec"')
    assert (
        classify(
            "miniagent",
            "tool_descriptions/shell.json",
            before,
            after,
            "client_tool",
            "tool",
        )["component"]
        == "prompt"
    )
    after = before.replace('"object"', '"array"')
    assert (
        classify(
            "miniagent",
            "tool_descriptions/shell.json",
            before,
            after,
            "client_tool",
            "tool",
        )["component"]
        == "client_tool"
    )


def test_declared_mechanisms_share_file_without_stealing_each_others_labels():
    before = "messages = history\ndone = False\n"
    after = "messages = trim_history(history)\ndone = True\nif done:\n    stop()\n"
    for tag in ("control_flow", "context_mgmt"):
        assert (
            classify("miniagent", "runtime/agent.py", before, after, tag)["component"]
            == tag
        )


def test_generic_unknown_is_not_prompt_and_explicit_signal_works():
    assert (
        classify("external", "extension.py", "flag=1\n", "flag=2\n", "memory")[
            "component"
        ]
        == UNKNOWN
    )
    assert (
        classify(
            "external",
            "extension.py",
            "flag=1\n",
            "flag=2\n",
            "memory",
            custom={"memory": [r"FILE extension\.py"]},
        )["component"]
        == "memory"
    )


def test_claude_does_not_advertise_inaccessible_memory_core():
    snapshot, _ = pair("claude_code", "CLAUDE.md", "old", "new", "prompt")
    assert set(available_components(snapshot, {})) == TAGS - {"memory"}


@pytest.mark.parametrize(
    "agent,path,component,comment",
    [
        ("codex", "python_core/code_mode_worker.js", "tool", "// Memory() subagent\n"),
        ("claude_code", "hooks/check.sh", "hook", "# Memory() subagent\n"),
    ],
)
def test_script_comments_do_not_count_as_runtime_changes(
    agent, path, component, comment
):
    original = (
        'echo "# not a comment"\n'
        if path.endswith(".sh")
        else 'run("// not a comment");\n'
    )
    result = classify(
        agent, path, original, original + comment, "client_tool", component
    )
    assert result["component"] == "prompt"


def test_claude_hook_structured_stdout_and_executable_activation():
    result = classify(
        "claude_code",
        "hooks/result.sh",
        "",
        "printf '%s' '{\"hookSpecificOutput\":{}}'\n",
        "output_plumbing",
        "hook",
    )
    assert result["component"] == "output_plumbing"
    before, after = pair(
        "claude_code", "hooks/result.sh", "echo ok\n", "echo ok\n", "hook"
    )
    after.files["hooks/result.sh"] = SnapshotFile(b"echo ok\n", "hook", executable=True)
    assert (
        classify_edit(
            before,
            after,
            {"component": "control_flow", "paths": ["hooks/result.sh"]},
            {},
        )["component"]
        == "control_flow"
    )


def test_reserved_candidate_reaches_evaluation_with_empty_signals(
    monkeypatch, tmp_path
):
    def edit(prompt, directory):
        if "Round 0," in prompt:
            (directory / "skills/main.md").write_text("UNHELPFUL")
            return [change()]
        p = directory / "tools/runtime.py"
        p.write_text(p.read_text() + "\nif True:\n    value += 1\n")
        return [change("client_tool", "tools/runtime.py")]

    algo, calls = setup(
        monkeypatch, tmp_path, edit, T=2, m=2, w=1, m_draft=1, component_signals={}
    )
    asyncio.run(algo.evolve())
    histories = saved(algo, "edit-history")
    reserved = next(row for row in histories if row["t"] == 1 and row["variant"] == 1)
    assert reserved["candidate_id"] is not None
    assert reserved["component"] == "client_tool"
    assert reserved["classification"]["profile"] == "miniagent"
    assert reserved["delta_S"] is not None
    assert len(calls) == 10


def test_classifier_version_cannot_reuse_legacy_phases(monkeypatch, tmp_path):
    old, _ = setup(
        monkeypatch, tmp_path / "old", lambda *_: [], component_classifier="legacy"
    )
    Checkpoints(old).start()
    new, _ = setup(monkeypatch, tmp_path / "new", lambda *_: [])
    with pytest.raises(ValueError, match="configuration/tasks changed"):
        CheckpointResume(old._run_dir).bind(new)
    legacy, _ = setup(
        monkeypatch, tmp_path / "legacy", lambda *_: [], component_classifier="legacy"
    )
    CheckpointResume(old._run_dir).bind(legacy)
