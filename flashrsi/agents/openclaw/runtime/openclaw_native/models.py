"""Small, provider-neutral contracts for the OpenClaw native runtime.

The upstream runtime publishes mutable JavaScript envelopes while streaming.
The snapshot uses plain dictionaries at the wire boundary and immutable event
copies at publication, which makes traces deterministic and safe to retain.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ModelResult:
    text: str = ""
    tool_calls: tuple[dict[str, Any], ...] = ()
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        # Events are retained in the trajectory and also handed to observers;
        # a callback must not be able to mutate the stored nested arguments or
        # tool receipt after publication.
        return {"type": self.type, **copy.deepcopy(self.data)}


def message(role: str, content: Any, **extra: Any) -> dict[str, Any]:
    result = {"role": role, "content": content}
    result.update(extra)
    return result


def content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for item in content:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                chunks.append(str(item.get("text", "")))
        return "".join(chunks)
    return "" if content is None else str(content)
