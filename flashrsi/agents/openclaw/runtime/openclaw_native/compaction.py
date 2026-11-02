"""Headless OpenClaw-compatible context compaction.

OpenClaw compacts at user-turn boundaries, protects a bounded recent tail and
can prune older tool output before building the compaction prompt. The
Snapshot cannot invoke OpenClaw's separate ``compaction`` agent, so the summary
text is deterministic and local, while the selection/pruning rules mirror the
upstream runner.
"""

from __future__ import annotations

import copy
import json
from typing import Any

from .models import content_text

PRUNE_MINIMUM = 20_000
PRUNE_PROTECT = 40_000
MIN_PRESERVE_RECENT_TOKENS = 2_000
MAX_PRESERVE_RECENT_TOKENS = 15_000
PRUNE_PROTECTED_TOOLS = {"skill"}
TOOL_OUTPUT_MAX_CHARS = 2_000


def estimate_tokens(messages: list[dict]) -> int:
    total = 0
    for item in messages:
        text = content_text(item.get("content"))
        calls = item.get("tool_calls")
        if calls:
            text += json.dumps(calls, ensure_ascii=False, separators=(",", ":"))
        total += max(1, len(text) // 4)
    return total


def _serialize(item: dict[str, Any]) -> str:
    """Serialize a transcript message in the shape of OpenClaw's summary input."""

    role = str(item.get("role", ""))
    if role == "user":
        text = content_text(item.get("content")).strip()
        return f"[User]: {text}" if text else ""
    if role == "assistant":
        rows: list[str] = []
        text = content_text(item.get("content")).strip()
        if text:
            rows.append(f"[Assistant]: {text}")
        for call in item.get("tool_calls") or []:
            rows.append(
                "[Assistant tool call]: "
                + f"{call.get('name', '')}({json.dumps(call.get('arguments', {}), ensure_ascii=False, separators=(',', ':'))})"
            )
        return "\n".join(rows)
    if role == "tool":
        # OpenClaw stores a tool call and its completed result in one message
        # part.  The Snapshot keeps the assistant call and tool result as two
        # provider messages, so the call is serialized by the preceding
        # assistant envelope and must not be repeated here.
        if item.get("compacted"):
            return "[Tool result]: [Old tool result content cleared]"
        value = content_text(item.get("content"))
        receipt = item.get("receipt")
        if isinstance(receipt, dict) and not receipt.get("ok", True):
            error = str(receipt.get("error", value))
            return f"[Tool error]: {error}" if error else ""
        if isinstance(receipt, dict) or isinstance(item.get("attachments"), list):
            attachments = item.get("attachments")
            if not isinstance(attachments, list):
                attachments = receipt.get("attachments")
            if isinstance(attachments, list):
                labels = [
                    f"[Attached {attachment.get('mime')}: {attachment.get('filename', 'file')}]"
                    for attachment in attachments
                    if isinstance(attachment, dict) and attachment.get("mime")
                ]
                if labels:
                    value = "\n".join([value, *labels]) if value else "\n".join(labels)
        if not value:
            return ""
        if len(value) > TOOL_OUTPUT_MAX_CHARS:
            value = value[:TOOL_OUTPUT_MAX_CHARS] + "\n[truncated]"
        return f"[Tool result]: {value}"
    text = content_text(item.get("content")).strip()
    return f"[{role.title()}]: {text}" if text else ""


def _turns(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
    starts = [i for i, item in enumerate(messages) if item.get("role") == "user"]
    if not starts:
        return [(0, len(messages))] if messages else []
    return [
        (start, starts[index + 1] if index + 1 < len(starts) else len(messages))
        for index, start in enumerate(starts)
    ]


def _normalize_tail_start(messages: list[dict[str, Any]], start: int) -> int:
    """Never leave a tool result without its assistant call envelope."""

    if start >= len(messages):
        return len(messages)
    while start > 0 and messages[start].get("role") == "tool":
        start -= 1
    if (
        start > 0
        and messages[start].get("role") == "assistant"
        and messages[start].get("tool_calls")
    ):
        start -= 1
    return start


def _preserve_recent_start(
    messages: list[dict[str, Any]],
    *,
    budget: int,
    tail_turns: int | None,
) -> int:
    turns = _turns(messages)
    if not turns:
        return 0
    recent = turns[-tail_turns:] if tail_turns is not None else turns
    total = 0
    keep_start = len(messages)
    for start, end in reversed(recent):
        size = estimate_tokens(messages[start:end])
        if total + size <= budget:
            total += size
            keep_start = start
            continue
        remaining = budget - total
        for candidate in range(start, end):
            if estimate_tokens(messages[candidate:end]) <= remaining:
                keep_start = candidate
                break
        break
    # When the newest turn itself exceeds the tail budget and cannot be split,
    # upstream leaves ``tail_start_id`` unset and compacts the whole history.
    # Keep ``len(messages)`` here to represent an empty protected tail.
    return _normalize_tail_start(messages, keep_start)


def prune_tool_results(
    messages: list[dict[str, Any]],
    *,
    protect_tokens: int = PRUNE_PROTECT,
    minimum_tokens: int = PRUNE_MINIMUM,
) -> list[dict[str, Any]]:
    """Clear old, large tool outputs while retaining recent turns."""

    result = copy.deepcopy(messages)
    turns_seen = 0
    total = 0
    candidates: list[tuple[dict[str, Any], int]] = []
    for item in reversed(result):
        if item.get("role") == "user":
            turns_seen += 1
        # Walking backwards, the first user boundary marks the newest turn;
        # tool results encountered after that boundary belong to older turns
        # and are eligible for pruning. Requiring two boundaries skipped the
        # immediately preceding turn entirely.
        # Upstream starts pruning only after the newest complete user turn;
        # the most recent turn remains fully replayable.  A tool result is
        # therefore eligible only after crossing two user boundaries while
        # walking backwards.
        if turns_seen < 2 or item.get("role") != "tool":
            continue
        if item.get("compacted"):
            # The upstream walk stops at an already compacted result because
            # everything older has already been considered for pruning.
            break
        if str(item.get("name", "")) in PRUNE_PROTECTED_TOOLS:
            continue
        value = content_text(item.get("content"))
        size = max(1, len(value) // 4)
        total += size
        if total > protect_tokens:
            candidates.append((item, size))
    reclaim = sum(size for _, size in candidates)
    if reclaim <= minimum_tokens:
        return result
    for item, _ in candidates:
        item["content"] = "[Old tool result content cleared]"
        item["compacted"] = True
    return result


def compact(
    messages: list[dict],
    *,
    max_tokens: int,
    keep_recent: int = 8,
    preserve_recent_tokens: int | None = None,
    tail_turns: int | None = None,
    prune: bool = False,
) -> list[dict]:
    """Compact messages using OpenClaw's turn and protected-tail rules."""

    working = prune_tool_results(messages) if prune else copy.deepcopy(messages)
    if estimate_tokens(working) <= max_tokens:
        return working

    head_count = 0
    while head_count < len(working) and working[head_count].get("role") == "system":
        head_count += 1
    body = working[head_count:]
    if not body:
        return working
    if preserve_recent_tokens is None:
        preserve_recent_tokens = min(
            MAX_PRESERVE_RECENT_TOKENS,
            max(MIN_PRESERVE_RECENT_TOKENS, max_tokens // 4),
        )
    preserve_recent_tokens = max(1, int(preserve_recent_tokens))
    if tail_turns is None and keep_recent > 0:
        turns = _turns(body)
        tail_turns = min(keep_recent, len(turns)) if turns else None
    # tail_turns=0 means there is no protected tail: OpenClaw selects the
    # entire history for the compaction prompt.  ``tail_start`` is the first
    # retained item, so it points after the body in this case.
    tail_start = (
        len(body)
        if tail_turns == 0
        else _preserve_recent_start(
            body, budget=preserve_recent_tokens, tail_turns=tail_turns
        )
    )
    middle = body[:tail_start]
    tail = body[tail_start:]
    summary = "\n".join(filter(None, (_serialize(item) for item in middle)))
    summary_message = {
        # SessionCompaction stores the generated summary as a completed
        # assistant message. Keeping that role matters for provider adapters
        # that enforce user/assistant alternation after a compacted tail.
        "role": "assistant",
        "content": "[OpenClaw compacted prior context]\n" + summary,
    }
    result = (
        copy.deepcopy(working[:head_count]) + [summary_message] + copy.deepcopy(tail)
    )

    while estimate_tokens(result) > max_tokens and len(summary_message["content"]) > 64:
        current = summary_message["content"]
        # Preserve the first and last user anchors. A prefix-only slice can
        # erase the newest turn when the budget is small, while OpenClaw's
        # generated summary keeps both ends of the conversation salient.
        first = current.find("[User]:")
        last = current.rfind("[User]:")
        target_chars = max(64, max_tokens * 4)
        marker = "\n...[summary truncated]...\n"
        if first >= 0 and last > first:
            header = current[:first]
            available = max(2, target_chars - len(header) - len(marker))
            edge = max(1, available // 2)
            candidate = (
                header
                + current[first : first + edge]
                + marker
                + current[last : last + edge]
            )
            if len(candidate) < len(current):
                summary_message["content"] = candidate
                continue
        keep = max(64, len(current) // 2)
        summary_message["content"] = current[:keep]
    while estimate_tokens(result) > max_tokens:
        candidates = [
            item
            for item in result
            if content_text(item.get("content")) and item is not summary_message
        ]
        if not candidates:
            break
        target = max(
            candidates, key=lambda item: len(content_text(item.get("content")))
        )
        text = content_text(target.get("content"))
        # ``estimate_tokens`` floors short strings to one token.  A one-byte
        # value can therefore remain over budget forever when the requested
        # budget is zero or below one token; remove it entirely so compaction
        # always makes progress.
        if len(text) <= 1:
            target["content"] = ""
        else:
            target["content"] = text[: max(1, len(text) // 2)]
    return result
