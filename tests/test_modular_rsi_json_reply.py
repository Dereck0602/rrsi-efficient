import json
import re
from types import SimpleNamespace

import pytest

from flashrsi.algorithms.modular_rsi import ModularRSI, _json_reply
from flashrsi.algorithms.modular_rsi._prompts import ANALYZE
from flashrsi.core import Task


@pytest.mark.parametrize("wrapper", ["{}", "```json\n{}\n```", "```\n{}\n```"])
@pytest.mark.parametrize(
    "value",
    [
        {
            "accept": False,
            "reason": "Reject rewriting '```python\\n# comment\\nprint(1)\\n```'.",
        },
        [
            {
                "target": "runtime/agent.py",
                "task_ids": ["e0"],
                "rationale": "Preserve ``` inside JSON strings.",
                "change": "Keep interfaces.",
            }
        ],
        [],
    ],
)
def test_json_reply_preserves_backticks_inside_strings(wrapper, value):
    assert _json_reply(wrapper.format(json.dumps(value))) == value


@pytest.mark.parametrize(
    "reply",
    ["", "not JSON", "```json\n{}", "```json\n{}\n```\ntrailing prose", "{} {}"],
)
def test_json_reply_does_not_repair_invalid_json(reply):
    with pytest.raises(json.JSONDecodeError):
        _json_reply(reply)


def test_analysis_prompt_example_obeys_real_findings_protocol():
    prompt = ANALYZE.format(module="tool_use", files="[]", evidence="[]", history="[]")
    instructions = prompt.split("Return JSON only:", 1)[1]
    start = re.search(r"(?m)^[\[{]", instructions)
    assert start is not None
    example, _ = json.JSONDecoder().raw_decode(instructions[start.start() :])
    value = _json_reply(json.dumps(example))
    assert isinstance(value, list)
    reply = json.dumps(value)
    finding = value[0]
    algorithm = SimpleNamespace(
        config=SimpleNamespace(modules={"tool_use": [finding["target"]]}, edit_budget=1)
    )
    tasks = (Task(finding["task_ids"][0], "example task"),)
    rows = ModularRSI._findings(algorithm, reply, "tool_use", tasks)
    assert len(rows) == 1
    assert rows[0]["target"] == finding["target"]
    assert rows[0]["task_ids"] == finding["task_ids"]
    assert ModularRSI._findings(algorithm, "[]", "tool_use", tasks) == []
    with pytest.raises(ValueError, match="list of findings"):
        ModularRSI._findings(algorithm, json.dumps(finding), "tool_use", tasks)
