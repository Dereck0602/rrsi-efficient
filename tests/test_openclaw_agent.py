"""Contract tests for the pure-Python OpenClaw Target adapter."""

import pytest

from flashrsi.agents.openclaw import OpenClaw, _validate


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


def test_openclaw_is_registered_as_a_python_snapshot():
    agent = OpenClaw(_config())
    assert agent.name == "openclaw"
    assert "runtime/openclaw_native/__init__.py" in agent.snapshot.files
    assert "runtime/openclaw_native/README.md" in agent.snapshot.files


def test_openclaw_rejects_host_runtimes():
    with pytest.raises(ValueError, match="node_path"):
        _validate(_config(node_path="node"))
    with pytest.raises(ValueError, match="cli_path"):
        _validate(_config(cli_path="openclaw"))
    with pytest.raises(ValueError, match="gateway_path"):
        _validate(_config(gateway_path="gateway"))


def test_openclaw_requires_explicit_model_credentials():
    with pytest.raises(ValueError, match="model_name"):
        _validate(_config(llm={"api_key": "secret"}))
    with pytest.raises(ValueError, match="api_key"):
        _validate(_config(llm={"model_name": "test"}))


def test_openclaw_adapter_loads_selected_snapshot_runtime(tmp_path, monkeypatch):
    # The runtime test itself belongs to the native-core test module; this
    # assertion verifies the adapter uses the selected Snapshot boundary.
    agent = OpenClaw(_config())
    seed = agent.snapshot
    with seed.check_out() as files:
        path = files / "SOUL.md"
        path.write_text(path.read_text() + "\nADAPTER_SNAPSHOT_MARKER\n")
        selected = seed.commit(files)
    agent.update(selected)
    assert agent.snapshot.id == selected.id
    assert "ADAPTER_SNAPSHOT_MARKER" in selected.files["SOUL.md"].content.decode()
