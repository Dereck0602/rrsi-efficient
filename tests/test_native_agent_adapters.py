"""Contract tests shared by the OpenClaw and OpenCode adapters."""

import pytest

from flashrsi.agents.openclaw import OpenClaw
from flashrsi.agents.openclaw import _validate as validate_openclaw
from flashrsi.agents.opencode import OpenCode
from flashrsi.agents.opencode import _validate as validate_opencode


@pytest.fixture(
    params=[
        pytest.param((OpenClaw, validate_openclaw), id="openclaw"),
        pytest.param((OpenCode, validate_opencode), id="opencode"),
    ]
)
def adapter(request):
    return request.param


def _config(**overrides):
    return {
        "timeout": 10,
        "max_turns": 4,
        "llm": {
            "model_name": "openai/test",
            "api_key": "secret",
            "base_url": "https://provider.test/v1",
            "model_api": "chat_completions",
        },
        **overrides,
    }


def test_adapter_exposes_native_snapshot(adapter):
    cls, _ = adapter
    agent = cls(_config())
    name = cls.__name__.lower()
    assert agent.name == name
    runtime = f"runtime/{name}_native"
    assert f"{runtime}/__init__.py" in agent.snapshot.files
    assert f"{runtime}/README.md" in agent.snapshot.files
    if cls is OpenCode:
        assert f"{runtime}/PROVENANCE.json" in agent.snapshot.files


def test_adapter_rejects_host_runtime_paths(adapter):
    cls, validate = adapter
    extra_path = "gateway_path" if cls is OpenClaw else "bun_path"
    for field in ("node_path", "cli_path", extra_path):
        with pytest.raises(ValueError, match=field):
            validate(_config(**{field: "host-executable"}))


def test_adapter_requires_explicit_model_credentials(adapter):
    _, validate = adapter
    with pytest.raises(ValueError, match="model_name"):
        validate(_config(llm={"api_key": "secret"}))
    with pytest.raises(ValueError, match="api_key"):
        validate(_config(llm={"model_name": "test"}))


def test_adapter_selects_edited_snapshot(adapter):
    cls, _ = adapter
    agent = cls(_config())
    seed = agent.snapshot
    resource = "SOUL.md" if cls is OpenClaw else "runtime/opencode_native/README.md"
    with seed.check_out() as files:
        path = files / resource
        path.write_text(
            path.read_text(encoding="utf-8") + "\nADAPTER_SNAPSHOT_MARKER\n",
            encoding="utf-8",
        )
        selected = seed.commit(files)
    agent.update(selected)
    assert agent.snapshot.id == selected.id
    assert b"ADAPTER_SNAPSHOT_MARKER" in selected.files[resource].content
