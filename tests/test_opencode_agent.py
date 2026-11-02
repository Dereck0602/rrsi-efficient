"""Contract tests for the pure-Python OpenCode Target adapter."""

import pytest

from flashrsi.agents.opencode import OpenCode, _validate


def _config(**overrides):
    value = {
        "timeout": 10,
        "max_turns": 4,
        "llm": {
            "model_name": "openai/test",
            "api_key": "secret",
            "base_url": "https://provider.test/v1",
            "model_api": "chat_completions",
        },
    }
    value.update(overrides)
    return value


def test_opencode_is_registered_as_a_python_snapshot():
    agent = OpenCode(_config())
    assert agent.name == "opencode"
    assert "runtime/opencode_native/__init__.py" in agent.snapshot.files
    assert "runtime/opencode_native/README.md" in agent.snapshot.files
    assert "runtime/opencode_native/PROVENANCE.json" in agent.snapshot.files


def test_opencode_rejects_host_runtimes():
    with pytest.raises(ValueError, match="node_path"):
        _validate(_config(node_path="node"))
    with pytest.raises(ValueError, match="cli_path"):
        _validate(_config(cli_path="opencode"))
    with pytest.raises(ValueError, match="bun_path"):
        _validate(_config(bun_path="bun"))


def test_opencode_requires_explicit_model_credentials():
    with pytest.raises(ValueError, match="model_name"):
        _validate(_config(llm={"api_key": "secret"}))
    with pytest.raises(ValueError, match="api_key"):
        _validate(_config(llm={"model_name": "test"}))


def test_opencode_adapter_loads_selected_snapshot_runtime(tmp_path):
    agent = OpenCode(_config())
    seed = agent.snapshot
    with seed.check_out() as files:
        path = files / "runtime/opencode_native/README.md"
        path.write_text(path.read_text() + "\nADAPTER_SNAPSHOT_MARKER\n")
        selected = seed.commit(files)
    agent.update(selected)
    assert agent.snapshot.id == selected.id
    assert (
        "ADAPTER_SNAPSHOT_MARKER"
        in selected.files["runtime/opencode_native/README.md"].content.decode()
    )
