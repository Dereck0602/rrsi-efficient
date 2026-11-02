"""Native OpenCode tools with deterministic receipts and bounded authority."""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import time
import urllib.parse
import uuid
from collections.abc import Mapping
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .policy import ToolPolicy
from .resources import load as load_resources

_READ_DEFAULT_LIMIT = 2000
_READ_MAX_LINE_LENGTH = 2000
_READ_MAX_BYTES = 50 * 1024
_READ_BINARY_EXTENSIONS = {
    ".zip",
    ".tar",
    ".gz",
    ".exe",
    ".dll",
    ".so",
    ".class",
    ".jar",
    ".war",
    ".7z",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".bin",
    ".dat",
    ".obj",
    ".o",
    ".a",
    ".lib",
    ".wasm",
    ".pyc",
    ".pyo",
}
_READ_IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
_WEBFETCH_MAX_BYTES = 5 * 1024 * 1024


class _HTMLTextParser(HTMLParser):
    """Small stdlib HTML reader for webfetch text/markdown modes."""

    _SKIP = {"script", "style", "meta", "link", "noscript", "iframe", "object", "embed"}
    _BLOCK = {
        "address",
        "article",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "pre",
        "section",
        "tr",
    }

    def __init__(self, markdown: bool = False):
        super().__init__(convert_charrefs=True)
        self.markdown = markdown
        self.parts: list[str] = []
        self.skip = 0
        self.href: str | None = None

    def handle_starttag(self, tag: str, attrs):
        tag = tag.lower()
        if tag in self._SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in self._BLOCK:
            self.parts.append("\n")
        if self.markdown and tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("#" * int(tag[1]) + " ")
        if self.markdown and tag == "li":
            self.parts.append("- ")
        if self.markdown and tag == "a":
            self.href = dict(attrs).get("href")
            self.parts.append("[")
        if self.markdown and tag == "code":
            self.parts.append("`")

    def handle_endtag(self, tag: str):
        tag = tag.lower()
        if tag in self._SKIP and self.skip:
            self.skip -= 1
            return
        if self.skip:
            return
        if self.markdown and tag == "a" and self.href:
            self.parts.append(f"]({self.href})")
            self.href = None
        if self.markdown and tag == "code":
            self.parts.append("`")
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str):
        if not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        value = unescape("".join(self.parts))
        lines = [" ".join(line.split()) for line in value.splitlines()]
        return "\n".join(line for line in lines if line).strip()

    def feed_and_text(self, value: str) -> str:
        self.feed(value)
        self.close()
        return self.text()


def _webfetch_format(content: str, content_type: str, requested: str | None) -> str:
    if content_type.lower().split(";", 1)[0].strip() not in {
        "text/html",
        "application/xhtml+xml",
    }:
        return content
    if requested == "html":
        return content
    return _HTMLTextParser(markdown=requested != "text").feed_and_text(content)


def _read_mime(path: Path, sample: bytes) -> str:
    """Best-effort equivalent of OpenCode's sniffAttachmentMime."""

    if sample.startswith(b"%PDF-") or path.suffix.lower() == ".pdf":
        return "application/pdf"
    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if sample.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if sample.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if sample.startswith(b"RIFF") and sample[8:12] == b"WEBP":
        return "image/webp"
    return mimetypes.guess_type(path.name)[0] or ""


def _read_is_binary(path: Path, sample: bytes) -> bool:
    if path.suffix.lower() in _READ_BINARY_EXTENSIONS:
        return True
    if not sample:
        return False
    # OpenCode treats any NUL byte as a binary marker.  Do this before the
    # printable-byte ratio check: a single NUL in an otherwise textual file
    # must still produce the same binary-file error as the upstream tool.
    if b"\x00" in sample:
        return True
    non_printable = sum(
        1 for value in sample if value == 0 or value < 9 or (value > 13 and value < 32)
    )
    return non_printable / len(sample) > 0.3


def schemas(*, include_compat: bool = False) -> list[dict[str, Any]]:
    result = [
        {
            "name": "read",
            "description": "Read a UTF-8 file (OpenCode read)",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string"},
                    "path": {"type": "string"},
                    # OpenCode's NonNegativeInt accepts zero for both fields;
                    # offset zero is normalized to the first line by the
                    # runtime while limit zero produces an empty selection.
                    "offset": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "minimum": 0},
                },
                "required": ["filePath"],
            },
        },
        {
            "name": "write",
            "description": "Write a UTF-8 file (OpenCode write)",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string"},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["filePath", "content"],
            },
        },
        {
            "name": "edit",
            "description": "Replace text in a file (OpenCode edit)",
            "parameters": {
                "type": "object",
                "properties": {
                    "filePath": {"type": "string"},
                    "path": {"type": "string"},
                    "oldString": {"type": "string"},
                    "newString": {"type": "string"},
                    "replaceAll": {"type": "boolean"},
                    "oldText": {"type": "string"},
                    "newText": {"type": "string"},
                },
                "required": ["filePath", "oldString", "newString"],
            },
        },
        {
            "name": "apply_patch",
            "description": "Apply a patch to workspace files",
            "parameters": {
                "type": "object",
                "properties": {
                    "patchText": {"type": "string"},
                    "patch": {"type": "string"},
                },
                "required": ["patchText"],
            },
        },
        {
            "name": "bash",
            "description": "Run a shell command in the workspace",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "workdir": {"type": "string"},
                    "timeout": {"type": "number"},
                    "description": {"type": "string"},
                },
                "required": ["command"],
            },
        },
        {
            "name": "glob",
            "description": "Find files matching a glob pattern",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "grep",
            "description": "Search text in workspace files",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "include": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "fetch",
            "description": "Fetch a URL as text",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "timeout": {"type": "number"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "webfetch",
            "description": "Fetch a URL as text (OpenCode webfetch)",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "format": {"type": "string", "enum": ["markdown", "text", "html"]},
                    "timeout": {"type": "number"},
                },
                "required": ["url"],
            },
        },
        {
            "name": "diagnostics",
            "description": "Return lightweight file diagnostics",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "filePath": {"type": "string"},
                },
                "required": [],
            },
        },
        {
            "name": "task",
            "description": "Delegate a bounded subtask to an embedded agent",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "prompt": {"type": "string"},
                    "subagent_type": {"type": "string"},
                    "task_id": {"type": "string"},
                    "command": {"type": "string"},
                    "background": {"type": "boolean"},
                },
                "required": ["description", "prompt", "subagent_type"],
            },
        },
        {
            "name": "todowrite",
            "description": "Replace the session todo list",
            "parameters": {
                "type": "object",
                "properties": {"todos": {"type": "array", "items": {"type": "object"}}},
                "required": ["todos"],
            },
        },
        {
            "name": "skill",
            "description": "Load a named Snapshot skill",
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
    ]
    if include_compat:
        result.extend(
            [
                {
                    "name": "patch",
                    "description": "Compatibility alias for apply_patch",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "patch": {"type": "string"},
                            "patchText": {"type": "string"},
                        },
                        "required": ["patch"],
                    },
                },
                {
                    "name": "exec",
                    "description": "Compatibility alias for bash",
                    "parameters": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                        "required": ["command"],
                    },
                },
                {
                    "name": "process",
                    "description": "Inspect a background process",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string"},
                            "command": {"type": "string"},
                            "sessionId": {"type": "string"},
                        },
                        "required": ["action"],
                    },
                },
                {
                    "name": "read_skill",
                    "description": "Read a Snapshot skill instruction file",
                    "parameters": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                    },
                },
                {
                    "name": "memory_search",
                    "description": "Search workspace memory",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string"},
                            "offset": {"type": "integer"},
                            "limit": {"type": "integer"},
                        },
                        "required": ["query"],
                    },
                },
            ]
        )
    return result


def _relative(path: Path, workspace: Path) -> str:
    try:
        return path.relative_to(workspace).as_posix()
    except ValueError:
        return path.as_posix()


def _error(
    tool: str, message: str, *, error_type: str = "tool_error", **extra: Any
) -> dict[str, Any]:
    return {
        "ok": False,
        "tool": tool,
        "error": message,
        "error_type": error_type,
        **extra,
    }


class ToolRunner:
    def __init__(self, workspace: Path, root: Path, policy: ToolPolicy):
        self.workspace, self.root, self.policy = (
            Path(workspace).resolve(),
            Path(root).resolve(),
            policy,
        )
        # OpenCode attaches project instruction files that are below the
        # session root to the first read of a file in that subtree. Track the
        # claims per runner so repeated reads in one turn do not duplicate
        # the same system-reminder block. Root/ancestor instructions are
        # already part of the initial system prompt and are excluded below.
        self._claimed_instructions: set[str] = set()
        try:
            self._system_instruction_paths = set(
                load_resources(self.root, self.workspace, self.policy.config).files
            )
        except (OSError, UnicodeError):
            self._system_instruction_paths = set()
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._process_tasks: dict[str, asyncio.Task] = {}
        self._process_output: dict[str, str] = {}
        self._todos: list[dict[str, Any]] = []
        # Optional callback used by a Target to embed a child OpenCode turn.
        # Keeping it absent makes task deterministic and avoids host services.
        self.task_runner = self.policy.config.get("task_runner")

    def _read_instructions(self, path: Path) -> list[dict[str, str]]:
        """Resolve nested AGENTS/CLAUDE/CONTEXT files for a read result.

        ``Instruction.resolve`` in upstream OpenCode walks upward from the
        file's parent, selecting the first instruction filename at each level,
        and claims each path once per assistant message.  The initial system
        prompt already contains workspace/ancestor files, so only nested
        files are returned here.
        """

        loaded: list[dict[str, str]] = []
        current = path.parent
        try:
            inside_workspace = (
                current == self.workspace or self.workspace in current.parents
            )
        except (OSError, ValueError):
            inside_workspace = False
        if not inside_workspace:
            return loaded
        while current != self.workspace and self.workspace in current.parents:
            found: Path | None = None
            for name in ("AGENTS.md", "CLAUDE.md", "CONTEXT.md"):
                candidate = current / name
                try:
                    if candidate.is_file():
                        found = candidate.resolve()
                        break
                except OSError:
                    continue
            if found is not None:
                key = str(found)
                if (
                    key != str(path)
                    and key not in self._system_instruction_paths
                    and key not in self._claimed_instructions
                ):
                    try:
                        content = found.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        content = ""
                    self._claimed_instructions.add(key)
                    if content:
                        loaded.append(
                            {
                                "filepath": key,
                                "content": f"Instructions from: {key}\n{content}",
                            }
                        )
            current = current.parent
        return loaded

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        pattern = args.get("filePath", args.get("path", args.get("workdir", "*")))
        self.policy.check(name, str(pattern))
        # OpenCode's public tool names are used in trajectories. Legacy names
        # remain accepted for evolved snapshots and old benchmark configs.
        if name == "patch":
            name = "apply_patch"
        if name == "bash":
            # OpenCode exposes bash timeout in milliseconds; the compatibility
            # exec tool keeps the historical seconds unit.
            translated = dict(args)
            if "timeout" in translated:
                try:
                    translated["timeout"] = float(translated["timeout"]) / 1000.0
                except (TypeError, ValueError):
                    pass
            else:
                translated["timeout"] = 120.0
            result = await self._exec(
                {**translated, "command": args.get("command", "")}
            )
            result["tool"] = "bash"
            result.setdefault("content", result.get("output", ""))
            return result
        if name == "read":
            return self._read({**args, "path": args.get("filePath", args.get("path"))})
        if name == "write":
            return self._write({**args, "path": args.get("filePath", args.get("path"))})
        if name == "edit":
            return self._edit(
                {
                    **args,
                    "path": args.get("filePath", args.get("path")),
                    "oldText": args.get(
                        "oldString", args.get("oldText", args.get("old"))
                    ),
                    "newText": args.get(
                        "newString", args.get("newText", args.get("new"))
                    ),
                }
            )
        if name == "apply_patch":
            return self._patch(args)
        if name == "exec":
            return await self._exec(args)
        if name == "process":
            return await self._process(args)
        if name == "read_skill":
            return self._skill(args)
        if name == "memory_search":
            return self._memory(args)
        if name == "glob":
            return self._glob(args)
        if name == "grep":
            return self._grep(args)
        if name in {"fetch", "webfetch"}:
            fetch_args = (
                {**args, "format": "html"}
                if name == "fetch" and "format" not in args
                else args
            )
            result = await self._fetch(fetch_args)
            result["tool"] = name
            return result
        if name == "diagnostics":
            return self._diagnostics(args)
        if name == "task":
            return await self._task(args)
        if name == "todowrite":
            return self._todowrite(args)
        if name == "skill":
            result = self._skill(args)
            result["tool"] = "skill"
            return result
        raise ValueError(f"unknown tool: {name}")

    def _read(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            path = self.policy.path(args["path"])
            if not path.exists():
                suggestions: list[str] = []
                try:
                    needle = path.name.casefold()
                    suggestions = [
                        str(item)
                        for item in path.parent.iterdir()
                        if needle in item.name.casefold()
                        or item.name.casefold() in needle
                    ][:3]
                except OSError:
                    pass
                detail = f"File not found: {path}"
                if suggestions:
                    detail += "\n\nDid you mean one of these?\n" + "\n".join(
                        suggestions
                    )
                return _error("read", detail, error_type="not_found")
            if path.is_dir():
                entries = sorted(
                    item.name + ("/" if item.is_dir() else "")
                    for item in path.iterdir()
                )
                raw_offset = args.get("offset")
                raw_limit = args.get("limit", _READ_DEFAULT_LIMIT)
                # The upstream schema uses NonNegativeInt and normalizes a
                # falsy offset (including zero) to the first entry. Keep the
                # same validation here while avoiding an unhandled TypeError
                # when a compatibility caller supplies malformed JSON.
                if raw_offset is None:
                    offset = 1
                elif isinstance(raw_offset, bool) or not isinstance(raw_offset, int):
                    return _error(
                        "read",
                        "offset must be a non-negative integer",
                        error_type="invalid_arguments",
                    )
                elif raw_offset < 0:
                    return _error(
                        "read",
                        "offset must be a non-negative integer",
                        error_type="invalid_arguments",
                    )
                else:
                    offset = raw_offset or 1
                if raw_limit is None:
                    limit = _READ_DEFAULT_LIMIT
                elif isinstance(raw_limit, bool) or not isinstance(raw_limit, int):
                    return _error(
                        "read",
                        "limit must be a non-negative integer",
                        error_type="invalid_arguments",
                    )
                elif raw_limit < 0:
                    return _error(
                        "read",
                        "limit must be a non-negative integer",
                        error_type="invalid_arguments",
                    )
                else:
                    limit = raw_limit
                start = max(offset - 1, 0)
                selected = entries[start : start + limit]
                truncated = start + len(selected) < len(entries)
                listing = "\n".join(
                    [
                        f"<path>{path}</path>",
                        "<type>directory</type>",
                        "<entries>",
                        "\n".join(selected),
                        (
                            f"(Showing {len(selected)} of {len(entries)} entries. "
                            f"Use offset parameter to read beyond entry {offset + len(selected)})"
                            if truncated
                            else f"({len(entries)} entries)"
                        ),
                        "</entries>",
                    ]
                )
                return {
                    "ok": True,
                    "tool": "read",
                    "path": _relative(path, self.workspace),
                    "directory": True,
                    "entries": selected,
                    "total_entries": len(entries),
                    "offset": offset,
                    "truncated": truncated,
                    "content": listing,
                    "output": listing,
                    "metadata": {
                        "preview": "\n".join(selected[:20]),
                        "truncated": truncated,
                        "loaded": [],
                        "display": {
                            "type": "directory",
                            "path": str(path),
                            "entries": selected,
                            "offset": offset,
                            "totalEntries": len(entries),
                            "truncated": truncated,
                        },
                    },
                }
        except KeyError:
            return _error(
                "read",
                "missing required argument: path",
                error_type="invalid_arguments",
            )
        except PermissionError:
            raise
        except OSError as exc:
            return _error("read", str(exc), error_type=type(exc).__name__)

        loaded = self._read_instructions(path)
        try:
            raw_bytes = path.read_bytes()
        except OSError as exc:
            return _error("read", str(exc), error_type=type(exc).__name__)

        mime = _read_mime(path, raw_bytes[:4096])
        if mime in _READ_IMAGE_MIMES or mime == "application/pdf":
            kind = "PDF" if mime == "application/pdf" else "Image"
            content = f"{kind} read successfully"
            return {
                "ok": True,
                "tool": "read",
                "path": _relative(path, self.workspace),
                "content": content,
                "output": content,
                "metadata": {
                    "preview": content,
                    "truncated": False,
                    "loaded": [item["filepath"] for item in loaded],
                },
                "attachments": [
                    {
                        "type": "file",
                        "mime": mime,
                        "url": f"data:{mime};base64,{base64.b64encode(raw_bytes).decode('ascii')}",
                    }
                ],
                "truncated": False,
            }
        if _read_is_binary(path, raw_bytes[:4096]):
            return _error(
                "read",
                f"Cannot read binary file: {path}",
                error_type="binary_file",
            )
        # TextDecoder("utf-8") is non-fatal: malformed byte sequences are
        # replaced with U+FFFD. ``utf-8-sig`` also consumes a leading BOM,
        # matching the upstream decoder's default ignoreBOM behavior.
        text = raw_bytes.decode("utf-8-sig", errors="replace")

        try:
            raw_offset = args.get("offset")
            if raw_offset is not None and (
                isinstance(raw_offset, bool) or not isinstance(raw_offset, int)
            ):
                return _error(
                    "read",
                    "offset must be a non-negative integer",
                    error_type="invalid_arguments",
                )
            if isinstance(raw_offset, int) and raw_offset < 0:
                return _error(
                    "read",
                    "offset must be a non-negative integer",
                    error_type="invalid_arguments",
                )
            # The upstream schema accepts zero, but runtime defaults it to
            # the first line just as `params.offset || 1` does.
            offset = raw_offset or 1
            start = max(offset - 1, 0)
            limit = args.get("limit", _READ_DEFAULT_LIMIT)
            if limit is None:
                limit = _READ_DEFAULT_LIMIT
            if limit is not None and (
                isinstance(limit, bool) or not isinstance(limit, int)
            ):
                return _error(
                    "read",
                    "limit must be a non-negative integer",
                    error_type="invalid_arguments",
                )
            if limit is not None and limit < 0:
                return _error(
                    "read",
                    "limit must be a non-negative integer",
                    error_type="invalid_arguments",
                )
            lines = text.splitlines()
        except (TypeError, ValueError):
            return _error(
                "read",
                "offset and limit must be non-negative integers",
                error_type="invalid_arguments",
            )
        selected: list[str] = []
        byte_count = 0
        more = False
        cut = False
        # Stream.splitLines in OpenCode counts lines as it reads and stops as
        # soon as the 50 KB cap is reached. Preserve that observable count so
        # a very large file's ``totalLines`` and range errors match upstream.
        count = 0
        for source_line in lines:
            count += 1
            if count <= start:
                continue
            if limit is not None and len(selected) >= limit:
                more = True
                # OpenCode's streaming reader keeps consuming the stream after
                # the requested page is full so it can report the true file
                # line count in the footer.  It only stops early at the byte
                # cap, which is the observable difference for large files.
                continue
            line = source_line
            if len(line) > _READ_MAX_LINE_LENGTH:
                line = line[:_READ_MAX_LINE_LENGTH] + (
                    f"... (line truncated to {_READ_MAX_LINE_LENGTH} chars)"
                )
            size = len(line.encode("utf-8")) + (1 if selected else 0)
            if byte_count + size > _READ_MAX_BYTES:
                cut = True
                more = True
                break
            selected.append(line)
            byte_count += size
        if count < offset and not (count == 0 and offset == 1):
            return _error(
                "read",
                f"Offset {offset} is out of range for this file ({count} lines)",
                error_type="range_error",
            )
        normalized: list[str] = []
        for line in selected:
            if len(line) > 2000:
                normalized.append(line[:2000] + "... (line truncated to 2000 chars)")
            else:
                normalized.append(line)
        last = offset + len(normalized) - 1
        truncated = more or cut
        if cut:
            suffix = (
                f"(Output capped at {_READ_MAX_BYTES // 1024} KB. Showing lines "
                f"{start + 1}-{last}. Use offset={last + 1} to continue.)"
            )
        elif more:
            suffix = (
                f"(Showing lines {start + 1}-{last} of {count}. "
                f"Use offset={last + 1} to continue.)"
            )
        else:
            suffix = f"(End of file - total {count} lines)"
        output = "\n".join(
            [
                f"<path>{path}</path>",
                "<type>file</type>",
                "<content>",
                "\n".join(
                    f"{index + start + 1}: {line}"
                    for index, line in enumerate(normalized)
                ),
                suffix,
                "</content>",
            ]
        )
        if loaded:
            output += (
                "\n\n<system-reminder>\n"
                + "\n\n".join(item["content"] for item in loaded)
                + "\n</system-reminder>"
            )
        content = "\n".join(normalized)
        return {
            "ok": True,
            "tool": "read",
            "path": _relative(path, self.workspace),
            "content": content,
            "output": output,
            "line_start": start + 1,
            "line_end": last,
            "total_lines": count,
            "truncated": truncated,
            "metadata": {
                "preview": "\n".join(normalized[:20]),
                "truncated": truncated,
                "loaded": [item["filepath"] for item in loaded],
                "display": {
                    "type": "file",
                    "path": str(path),
                    "text": content,
                    "lineStart": start + 1,
                    "lineEnd": last,
                    "totalLines": count,
                    "truncated": truncated,
                },
            },
        }

    def _write(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            path = self.policy.path(args["path"])
            content = str(args["content"])
        except KeyError as exc:
            return _error(
                "write",
                f"missing required argument: {exc.args[0]}",
                error_type="invalid_arguments",
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except PermissionError:
            raise
        except (OSError, UnicodeError) as exc:
            return _error(
                "write",
                str(exc),
                error_type=type(exc).__name__,
                path=_relative(path, self.workspace),
            )
        return {
            "ok": True,
            "tool": "write",
            "path": _relative(path, self.workspace),
            "bytes": len(content.encode("utf-8")),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "output": "Wrote file successfully.",
        }

    def _edit(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            path = self.policy.path(args["path"])
            old_value = args.get("oldText", args.get("old"))
            new_value = args.get("newText", args.get("new"))
            if not isinstance(old_value, str) or not isinstance(new_value, str):
                raise KeyError("oldText/newText")
        except KeyError as exc:
            return _error(
                "edit",
                f"missing required argument: {exc.args[0]}",
                error_type="invalid_arguments",
            )
        except PermissionError:
            raise
        if old_value == new_value:
            return _error(
                "edit",
                "No changes to apply: oldString and newString are identical.",
                error_type="no_changes",
                path=_relative(path, self.workspace),
            )
        replace_all = bool(args.get("replaceAll", False))
        exists = path.exists()
        # OpenCode's edit tool uses an empty oldString as a file-creation form,
        # while rejecting an empty match against an existing file. This is
        # observable in both the write and error trajectories.
        if old_value == "":
            if exists:
                return _error(
                    "edit",
                    "oldString cannot be empty when editing an existing file. Provide the exact text to replace, or use write for an intentional full-file replacement.",
                    error_type="invalid_arguments",
                    path=_relative(path, self.workspace),
                )
            before = ""
            after = new_value
            count = 1
            source_bom = False
        else:
            if not exists:
                return _error(
                    "edit",
                    f"File {path} not found",
                    error_type="not_found",
                    path=_relative(path, self.workspace),
                )
            try:
                raw = path.read_bytes()
                source_bom = raw.startswith(b"\xef\xbb\xbf")
                before = raw.decode("utf-8-sig")
            except PermissionError:
                raise
            except (OSError, UnicodeError) as exc:
                return _error("edit", str(exc), error_type=type(exc).__name__)
            ending = "\r\n" if "\r\n" in before else "\n"
            normalized_before = before.replace("\r\n", "\n")
            old = old_value.replace("\r\n", "\n")
            new = new_value.replace("\r\n", "\n")
            count = normalized_before.count(old)
            if count != 1 and not (replace_all and count > 0):
                return _error(
                    "edit",
                    f"expected one match, found {count}",
                    error_type="match_error",
                    path=_relative(path, self.workspace),
                    matches=count,
                )
            after = normalized_before.replace(old, new, -1 if replace_all else 1)
            if ending == "\r\n":
                after = after.replace("\n", "\r\n")
        # A BOM in a newly supplied string is treated as a requested BOM, while
        # an existing BOM is preserved across edits.
        if after.startswith("\ufeff"):
            source_bom = True
            after = after[1:]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = after.encode("utf-8")
            path.write_bytes((b"\xef\xbb\xbf" if source_bom else b"") + data)
        except PermissionError:
            raise
        except (OSError, UnicodeError) as exc:
            return _error(
                "edit",
                str(exc),
                error_type=type(exc).__name__,
                path=_relative(path, self.workspace),
            )
        return {
            "ok": True,
            "tool": "edit",
            "path": _relative(path, self.workspace),
            "replacements": count if replace_all else 1,
            "sha256": hashlib.sha256(after.encode("utf-8")).hexdigest(),
            "output": "Edit applied successfully.",
        }

    def _patch(self, args: dict[str, Any]) -> dict[str, Any]:
        patch = str(args.get("patchText", args.get("patch", "")))
        if patch.lstrip().startswith("*** Begin Patch"):
            return self._native_patch(patch)
        lines = patch.splitlines(keepends=True)
        old_header = next((line for line in lines if line.startswith("--- ")), None)
        new_header = next((line for line in lines if line.startswith("+++ ")), None)
        if old_header is None or new_header is None:
            return _error(
                "apply_patch",
                "patch must contain --- and +++ file headers",
                error_type="invalid_patch",
            )
        old_name = old_header[4:].split("\t", 1)[0].strip()
        new_name = new_header[4:].split("\t", 1)[0].strip()
        target_name = new_name if new_name != "/dev/null" else old_name
        target_name = target_name.removeprefix("a/").removeprefix("b/")
        if target_name == "/dev/null" or not target_name:
            return _error(
                "apply_patch", "patch has no target", error_type="invalid_patch"
            )
        try:
            path = self.policy.path(target_name)
            exists = path.exists()
            before = path.read_text(encoding="utf-8") if exists else ""
        except (OSError, UnicodeError, PermissionError) as exc:
            return _error("apply_patch", str(exc), error_type=type(exc).__name__)
        source = before.splitlines(keepends=True)
        hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
        hunks: list[tuple[int, int, int, int, list[str]]] = []
        index = 0
        while index < len(lines):
            match = hunk_re.match(lines[index].rstrip("\n"))
            if not match:
                index += 1
                continue
            old_start = int(match.group(1))
            old_count = int(match.group(2) or "1")
            new_start = int(match.group(3))
            new_count = int(match.group(4) or "1")
            index += 1
            body: list[str] = []
            while index < len(lines) and not hunk_re.match(lines[index].rstrip("\n")):
                if lines[index].startswith((" ", "+", "-", "\\")):
                    body.append(lines[index])
                index += 1
            hunks.append((old_start, old_count, new_start, new_count, body))
        if not hunks:
            return _error(
                "apply_patch", "patch contains no hunks", error_type="invalid_patch"
            )
        offset = 0
        output = list(source)
        for old_start, old_count, _new_start, new_count, body in hunks:
            removed = [line[1:] for line in body if line[:1] in (" ", "-")]
            replacement = [line[1:] for line in body if line[:1] in (" ", "+")]
            if len(removed) != old_count or len(replacement) != new_count:
                return _error(
                    "apply_patch",
                    "hunk line counts do not match header",
                    error_type="invalid_patch",
                )
            pos = max(0, old_start - 1 + offset)
            if output[pos : pos + len(removed)] != removed:
                return _error(
                    "apply_patch",
                    f"patch context not found at line {old_start}",
                    error_type="context_error",
                    path=_relative(path, self.workspace),
                )
            output[pos : pos + len(removed)] = replacement
            offset += len(replacement) - len(removed)
        after = "".join(output)
        try:
            if new_name == "/dev/null":
                if path.exists():
                    path.unlink()
                changed = bool(before)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(after, encoding="utf-8")
                changed = before != after
        except OSError as exc:
            return _error(
                "apply_patch",
                str(exc),
                error_type=type(exc).__name__,
                path=_relative(path, self.workspace),
            )
        return {
            "ok": True,
            "tool": "apply_patch",
            "path": _relative(path, self.workspace),
            "changed": changed,
            "hunks": len(hunks),
            "sha256": hashlib.sha256(after.encode("utf-8")).hexdigest()
            if new_name != "/dev/null"
            else None,
            "output": "Success. Updated the following files:\nM "
            + _relative(path, self.workspace),
        }

    def _native_patch(self, patch: str) -> dict[str, Any]:
        """Apply OpenCode's ``*** Begin Patch`` form for add/update/delete."""
        lines = patch.replace("\r\n", "\n").splitlines()
        if not lines or lines[-1].strip() != "*** End Patch":
            return _error(
                "apply_patch",
                "patch must end with *** End Patch",
                error_type="invalid_patch",
            )
        index = 1
        changed: list[str] = []
        while index < len(lines) - 1:
            header = lines[index]
            index += 1
            if header.startswith("*** Add File: "):
                name = header.removeprefix("*** Add File: ").strip()
                try:
                    path = self.policy.path(name)
                except PermissionError:
                    raise
                content: list[str] = []
                while index < len(lines) - 1 and not lines[index].startswith("*** "):
                    line = lines[index]
                    if not line.startswith("+"):
                        return _error(
                            "apply_patch",
                            "added file lines must start with +",
                            error_type="invalid_patch",
                        )
                    content.append(line[1:])
                    index += 1
                text = "\n".join(content) + ("\n" if content else "")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                changed.append(_relative(path, self.workspace))
                continue
            if header.startswith("*** Delete File: "):
                name = header.removeprefix("*** Delete File: ").strip()
                path = self.policy.path(name)
                if not path.is_file():
                    return _error(
                        "apply_patch", f"file not found: {name}", error_type="not_found"
                    )
                path.unlink()
                changed.append(_relative(path, self.workspace))
                continue
            if header.startswith("*** Update File: "):
                name = header.removeprefix("*** Update File: ").strip()
                path = self.policy.path(name)
                if not path.is_file():
                    return _error(
                        "apply_patch", f"file not found: {name}", error_type="not_found"
                    )
                move_path = None
                if index < len(lines) - 1 and lines[index].startswith("*** Move to: "):
                    move_path = self.policy.path(
                        lines[index].removeprefix("*** Move to: ").strip()
                    )
                    index += 1
                source = path.read_text(encoding="utf-8").splitlines()
                while index < len(lines) - 1 and not lines[index].startswith("*** "):
                    if lines[index].startswith("@@"):
                        index += 1
                    body: list[str] = []
                    while (
                        index < len(lines) - 1
                        and not lines[index].startswith("@@")
                        and not lines[index].startswith("*** ")
                    ):
                        body.append(lines[index])
                        index += 1
                    removed = [line[1:] for line in body if line[:1] in {" ", "-"}]
                    replacement = [line[1:] for line in body if line[:1] in {" ", "+"}]
                    if not removed and not replacement:
                        continue
                    try:
                        pos = next(
                            i
                            for i in range(len(source) - len(removed) + 1)
                            if source[i : i + len(removed)] == removed
                        )
                    except StopIteration:
                        return _error(
                            "apply_patch",
                            f"patch context not found: {name}",
                            error_type="context_error",
                        )
                    source[pos : pos + len(removed)] = replacement
                destination = move_path or path
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(
                    "\n".join(source) + ("\n" if source else ""), encoding="utf-8"
                )
                if move_path is not None:
                    path.unlink()
                changed.append(_relative(destination, self.workspace))
                continue
            return _error(
                "apply_patch",
                f"unknown patch header: {header}",
                error_type="invalid_patch",
            )
        if not changed:
            return _error(
                "apply_patch",
                "patch contains no file changes",
                error_type="invalid_patch",
            )
        return {
            "ok": True,
            "tool": "apply_patch",
            "files": changed,
            "changed": True,
            "output": "Success. Updated the following files:\n"
            + "\n".join(f"M {name}" for name in changed),
        }

    async def _exec(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            timeout = float(args.get("timeout", 30))
            if timeout <= 0:
                raise ValueError
            command = str(args["command"])
            workdir = self.policy.path(args.get("workdir", "."))
            if not workdir.is_dir():
                return _error(
                    "exec",
                    f"workdir is not a directory: {workdir}",
                    error_type="invalid_arguments",
                )
            max_output = max(
                0, int(args.get("max_output_chars", args.get("maxOutputChars", 0)))
            )
            extra_env = args.get("env") or {}
            if not isinstance(extra_env, dict):
                return _error(
                    "exec", "env must be an object", error_type="invalid_arguments"
                )
        except (KeyError, TypeError, ValueError):
            return _error(
                "exec",
                "command and a positive timeout are required",
                error_type="invalid_arguments",
            )
        try:
            yield_ms = max(0.0, float(args.get("yieldMs", 0)))
        except (TypeError, ValueError):
            return _error(
                "exec",
                "yieldMs must be a non-negative number",
                error_type="invalid_arguments",
            )
        if args.get("background") or yield_ms:
            started = await self._process(
                {
                    "action": "run",
                    "command": command,
                    "timeout": yield_ms / 1000 if yield_ms else 0,
                    "workdir": str(workdir),
                    "env": extra_env,
                }
            )
            started["tool"] = "exec"
            if (
                max_output
                and isinstance(started.get("output"), str)
                and len(started["output"]) > max_output
            ):
                started["output"] = started["output"][:max_output]
                started["truncated"] = True
            return started
        started = time.monotonic()
        try:
            env = os.environ.copy()
            env.update({str(key): str(value) for key, value in extra_env.items()})
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
                return _error(
                    "exec",
                    f"command timed out after {timeout:g}s",
                    error_type="timeout",
                    returncode=-1,
                    duration_ms=round((time.monotonic() - started) * 1000),
                )
        except OSError as exc:
            return _error("exec", str(exc), error_type=type(exc).__name__)
        output = out.decode("utf-8", "replace")
        truncated = False
        if max_output and len(output) > max_output:
            output = output[:max_output]
            truncated = True
        return {
            "ok": proc.returncode == 0,
            "tool": "exec",
            "output": output,
            "returncode": proc.returncode,
            "duration_ms": round((time.monotonic() - started) * 1000),
            "truncated": truncated,
        }

    async def _process(self, args: dict[str, Any]) -> dict[str, Any]:
        """Run and inspect a process within this turn's workspace scope."""
        action = str(args.get("action", "list")).lower()
        if action == "list":
            rows = []
            for session_id, task in self._process_tasks.items():
                if task.done():
                    await self._finish_process(session_id)
                rows.append(
                    {
                        "sessionId": session_id,
                        "running": not task.done(),
                        "returncode": self._processes[session_id].returncode,
                        "output": self._process_output.get(session_id, "")[-2000:],
                    }
                )
            output = "No active processes"
            if rows:
                output = "\n".join(
                    f"{row['sessionId']}: "
                    + ("running" if row["running"] else f"exited ({row['returncode']})")
                    + (f"\n{row['output']}" if row["output"] else "")
                    for row in rows
                )
            return {
                "ok": True,
                "tool": "process",
                "action": action,
                "sessions": rows,
                "output": output,
            }
        if action == "run":
            command = str(args.get("command", "")).strip()
            if not command:
                return _error(
                    "process", "command is required", error_type="invalid_arguments"
                )
            try:
                timeout = float(args.get("timeout", 0))
            except (TypeError, ValueError):
                return _error(
                    "process",
                    "timeout must be a number",
                    error_type="invalid_arguments",
                )
            if timeout < 0:
                return _error(
                    "process",
                    "timeout must be non-negative",
                    error_type="invalid_arguments",
                )
            extra_env = args.get("env") or {}
            if not isinstance(extra_env, Mapping):
                return _error(
                    "process", "env must be an object", error_type="invalid_arguments"
                )
            try:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=self.policy.path(args.get("workdir", ".")),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env={
                        **os.environ,
                        **{str(key): str(value) for key, value in extra_env.items()},
                    },
                )
            except OSError as exc:
                return _error("process", str(exc), error_type=type(exc).__name__)
            session_id = "proc-" + uuid.uuid4().hex[:12]
            self._processes[session_id] = proc
            self._process_output[session_id] = ""
            task = asyncio.create_task(self._collect_process(session_id))
            self._process_tasks[session_id] = task
            if timeout:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout)
                except asyncio.TimeoutError:
                    pass
            if task.done():
                await self._finish_process(session_id)
            return {
                "ok": True,
                "tool": "process",
                "action": action,
                "sessionId": session_id,
                "running": not task.done(),
                "output": self._process_output.get(session_id, ""),
                "returncode": proc.returncode,
            }
        session_id = str(args.get("sessionId", ""))
        if not session_id or session_id not in self._process_tasks:
            return _error("process", "unknown sessionId", error_type="not_found")
        task = self._process_tasks[session_id]
        proc = self._processes[session_id]
        if action == "poll":
            try:
                timeout = max(0.0, float(args.get("timeout", 0)))
            except (TypeError, ValueError):
                return _error(
                    "process",
                    "timeout must be a number",
                    error_type="invalid_arguments",
                )
            if not task.done() and timeout:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout)
                except asyncio.TimeoutError:
                    pass
            if task.done():
                await self._finish_process(session_id)
            return {
                "ok": True,
                "tool": "process",
                "action": action,
                "sessionId": session_id,
                "running": not task.done(),
                "returncode": proc.returncode,
                "output": self._process_output.get(session_id, ""),
            }
        if action == "log":
            if task.done():
                await self._finish_process(session_id)
            try:
                offset = max(0, int(args.get("offset", 0)))
                limit = max(0, int(args.get("limit", 2000)))
            except (TypeError, ValueError):
                return _error(
                    "process",
                    "offset and limit must be integers",
                    error_type="invalid_arguments",
                )
            output = self._process_output.get(session_id, "")
            return {
                "ok": True,
                "tool": "process",
                "action": action,
                "sessionId": session_id,
                "running": not task.done(),
                "content": output[offset : offset + limit],
                "offset": offset,
                "total": len(output),
                "output": output[offset : offset + limit],
            }
        if action == "write":
            data = args.get("data", "")
            if not isinstance(data, str):
                return _error(
                    "process", "data must be a string", error_type="invalid_arguments"
                )
            if proc.stdin is None or proc.returncode is not None:
                return _error("process", "process stdin is closed", error_type="closed")
            try:
                proc.stdin.write(data.encode("utf-8"))
                await proc.stdin.drain()
                if args.get("eof"):
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionError, OSError) as exc:
                return _error("process", str(exc), error_type="closed")
            return {
                "ok": True,
                "tool": "process",
                "action": action,
                "sessionId": session_id,
                "bytes": len(data.encode("utf-8")),
                "output": f"Wrote {len(data.encode('utf-8'))} bytes to process {session_id}.",
            }
        if action == "kill":
            if not task.done():
                proc.terminate()
                try:
                    await asyncio.wait_for(asyncio.shield(task), 2)
                except asyncio.TimeoutError:
                    proc.kill()
                    await task
            await self._finish_process(session_id)
            return {
                "ok": True,
                "tool": "process",
                "action": action,
                "sessionId": session_id,
                "running": False,
                "returncode": proc.returncode,
                "output": self._process_output.get(session_id, ""),
            }
        if action == "clear":
            self._process_output[session_id] = ""
            return {
                "ok": True,
                "tool": "process",
                "action": action,
                "sessionId": session_id,
                "running": not task.done(),
                "output": f"Cleared output for process {session_id}.",
            }
        return _error(
            "process", f"unknown action: {action}", error_type="invalid_arguments"
        )

    async def _collect_process(self, session_id: str) -> None:
        proc = self._processes[session_id]
        stream = proc.stdout
        if stream is not None:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                self._process_output[session_id] = self._process_output.get(
                    session_id, ""
                ) + chunk.decode("utf-8", "replace")
        await proc.wait()

    async def _finish_process(self, session_id: str) -> None:
        task = self._process_tasks[session_id]
        if not task.done():
            return
        try:
            task.result()
        except (asyncio.CancelledError, OSError):
            return

    def _glob(self, args: dict[str, Any]) -> dict[str, Any]:
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return _error("glob", "pattern is required", error_type="invalid_arguments")
        base = str(args.get("path", "."))
        try:
            root = self.policy.path(base)
        except PermissionError:
            raise
        if not root.is_dir():
            return _error(
                "glob",
                f"path is not a directory: {base}",
                error_type="invalid_arguments",
            )
        # pathlib glob does not permit a caller to escape a sandbox through a
        # leading ../ pattern; every result is checked after resolution.
        try:
            all_matches = sorted(
                p
                for p in root.glob(pattern)
                if p.exists()
                and p.resolve().is_relative_to(
                    self.workspace if self.policy.sandbox else root.resolve()
                )
            )
        except (OSError, ValueError) as exc:
            return _error("glob", str(exc), error_type="invalid_arguments")
        truncated = len(all_matches) >= 100
        matches = all_matches[:100]
        rendered = [
            str(p.resolve() if p.is_absolute() else (root / p).resolve())
            for p in matches
        ]
        if not rendered:
            output = "No files found"
        else:
            output = "\n".join(rendered)
            if truncated:
                output += "\n\n(Results are truncated: showing first 100 results. Consider using a more specific path or pattern.)"
        return {
            "ok": True,
            "tool": "glob",
            "pattern": pattern,
            "path": _relative(root, self.workspace),
            "matches": [_relative(p.resolve(), self.workspace) for p in matches],
            "count": len(matches),
            "truncated": truncated,
            "output": output,
        }

    def _grep(self, args: dict[str, Any]) -> dict[str, Any]:
        pattern = str(args.get("pattern", ""))
        if not pattern:
            return _error("grep", "pattern is required", error_type="invalid_arguments")
        base = str(args.get("path", "."))
        include = str(args.get("include", "*"))
        try:
            root = self.policy.path(base)
        except PermissionError:
            raise
        candidates = (
            [root]
            if root.is_file()
            else list(root.rglob(include))
            if root.is_dir()
            else []
        )
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return _error(
                "grep",
                f"invalid regular expression: {exc}",
                error_type="invalid_arguments",
            )
        results: list[dict[str, Any]] = []
        for path in sorted(candidates):
            try:
                resolved = path.resolve()
                if not resolved.is_file() or (
                    self.policy.sandbox and not resolved.is_relative_to(self.workspace)
                ):
                    continue
                if path.is_dir() or not fnmatch.fnmatch(path.name, include):
                    continue
                text = resolved.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    results.append(
                        {
                            "path": _relative(resolved, self.workspace),
                            "line": lineno,
                            "text": line,
                        }
                    )
                    if len(results) >= 100:
                        break
            if len(results) >= 100:
                break
        truncated = len(results) >= 100
        if not results:
            output = "No files found"
        else:
            lines = [
                f"Found {len(results)} matches"
                + (" (more matches available)" if truncated else "")
            ]
            current = None
            for match in results:
                absolute = str((self.workspace / match["path"]).resolve())
                if absolute != current:
                    if current is not None:
                        lines.append("")
                    current = absolute
                    lines.append(f"{absolute}:")
                lines.append(f"  Line {match['line']}: {match['text']}")
            if truncated:
                lines.extend(
                    [
                        "",
                        "(Results truncated. Consider using a more specific path or pattern.)",
                    ]
                )
            output = "\n".join(lines)
        return {
            "ok": True,
            "tool": "grep",
            "pattern": pattern,
            "matches": results,
            "count": len(results),
            "truncated": truncated,
            "output": output,
        }

    async def _fetch(self, args: dict[str, Any]) -> dict[str, Any]:
        url = str(args.get("url", "")).strip()
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return _error(
                "fetch",
                "url must be an absolute http(s) URL",
                error_type="invalid_arguments",
            )
        try:
            # OpenCode defaults to 30 seconds and caps caller supplied values
            # at two minutes.  Keep the cap here so the native snapshot has
            # the same bounded request lifetime as webfetch.ts.
            timeout = min(float(args.get("timeout", 30)), 120)
            if timeout <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return _error(
                "fetch", "timeout must be positive", error_type="invalid_arguments"
            )
        try:
            import httpx

            async with httpx.AsyncClient(
                timeout=timeout, follow_redirects=True
            ) as client:
                response = await client.get(url)
                content_type = response.headers.get("content-type", "")
                raw = response.content
                requested = args.get("format")
                if requested is not None and requested not in {
                    "markdown",
                    "text",
                    "html",
                }:
                    return _error(
                        "fetch",
                        "format must be markdown, text, or html",
                        error_type="invalid_arguments",
                    )
                if not response.is_success:
                    return _error(
                        "fetch",
                        f"HTTP {response.status_code} while fetching {url}",
                        error_type="http_error",
                        url=url,
                        status=response.status_code,
                    )
                declared_length = response.headers.get("content-length")
                if declared_length:
                    try:
                        declared_size = int(declared_length)
                    except (TypeError, ValueError):
                        declared_size = 0
                    if declared_size > _WEBFETCH_MAX_BYTES:
                        return _error(
                            "fetch",
                            "Response too large (exceeds 5MB limit)",
                            error_type="response_too_large",
                            url=url,
                        )
                if len(raw) > _WEBFETCH_MAX_BYTES:
                    return _error(
                        "fetch",
                        "Response too large (exceeds 5MB limit)",
                        error_type="response_too_large",
                        url=url,
                    )
                mime = content_type.split(";", 1)[0].strip().lower()
                if mime in _READ_IMAGE_MIMES:
                    output = "Image fetched successfully"
                    return {
                        "ok": True,
                        "tool": "fetch",
                        "url": url,
                        "status": response.status_code,
                        "content_type": content_type,
                        "content": output,
                        "output": output,
                        "attachments": [
                            {
                                "type": "file",
                                "mime": mime,
                                "url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}",
                            }
                        ],
                    }
                text = raw.decode(response.encoding or "utf-8", "replace")
                output = _webfetch_format(text, content_type, requested)
                return {
                    "ok": response.is_success,
                    "tool": "fetch",
                    "url": url,
                    "status": response.status_code,
                    "content_type": content_type,
                    "content": output,
                    "output": output,
                }
        except Exception as exc:
            return _error("fetch", str(exc), error_type=type(exc).__name__, url=url)

    def _diagnostics(self, args: dict[str, Any]) -> dict[str, Any]:
        value = args.get("filePath", args.get("path"))
        if value:
            try:
                path = self.policy.path(str(value))
            except PermissionError:
                raise
            if not path.exists():
                diagnostics = [{"severity": "error", "message": "file does not exist"}]
                return {
                    "ok": True,
                    "tool": "diagnostics",
                    "path": str(value),
                    "diagnostics": diagnostics,
                    "output": f"{value}: error: file does not exist",
                }
            if path.is_dir():
                return {
                    "ok": True,
                    "tool": "diagnostics",
                    "path": str(value),
                    "diagnostics": [],
                    "output": "No diagnostics found",
                }
            try:
                path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                diagnostics = [{"severity": "warning", "message": str(exc)}]
                return {
                    "ok": True,
                    "tool": "diagnostics",
                    "path": str(value),
                    "diagnostics": diagnostics,
                    "output": f"{value}: warning: {exc}",
                }
            except OSError as exc:
                diagnostics = [{"severity": "error", "message": str(exc)}]
                return {
                    "ok": True,
                    "tool": "diagnostics",
                    "path": str(value),
                    "diagnostics": diagnostics,
                    "output": f"{value}: error: {exc}",
                }
        return {
            "ok": True,
            "tool": "diagnostics",
            "diagnostics": [],
            "output": "No diagnostics found",
        }

    async def _task(self, args: dict[str, Any]) -> dict[str, Any]:
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return _error("task", "prompt is required", error_type="invalid_arguments")
        runner = self.task_runner
        if not callable(runner):
            return _error(
                "task",
                "embedded task delegation is not configured",
                error_type="not_configured",
            )
        try:
            result = runner(prompt, dict(args))
            if hasattr(result, "__await__"):
                result = await result
            task_id = str(args.get("task_id") or "task-" + uuid.uuid4().hex[:12])
            if isinstance(result, Mapping):
                state = str(result.get("status") or result.get("state") or "completed")
                text = str(result.get("output") or result.get("text") or result)
                summary = result.get("summary")
            else:
                state = "completed"
                text = str(result)
                summary = None
            tag = "task_error" if state == "error" else "task_result"
            output_lines = [
                f'<task id="{task_id}" state="{state}">',
                *([f"<summary>{summary}</summary>"] if summary else []),
                f"<{tag}>",
                text,
                f"</{tag}>",
                "</task>",
            ]
            return {
                "ok": True,
                "tool": "task",
                "task_id": task_id,
                "state": state,
                "description": str(args.get("description", "")),
                "result": result,
                "output": "\n".join(output_lines),
            }
        except Exception as exc:
            return _error("task", str(exc), error_type=type(exc).__name__)

    def _skill(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name", ""))
        if (
            not name
            or Path(name).is_absolute()
            or ".." in name.replace("\\", "/").split("/")
        ):
            return _error(
                "read_skill",
                "skill name must be a relative name",
                error_type="invalid_arguments",
            )
        loaded = load_resources(self.root, self.workspace, self.policy.config)
        skill = loaded.skill(name)
        if skill is None:
            return _error(
                "read_skill", f"skill not found: {name}", error_type="not_found"
            )
        try:
            content = skill.path.read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            return _error("read_skill", str(exc), error_type=type(exc).__name__)
        # The public OpenCode skill tool returns the rendered envelope and a
        # small sampled file list, rather than exposing the raw frontmatter
        # document. Keep the body readable while preserving the skill's base
        # directory for relative references.
        body = content
        if body.startswith("---"):
            lines = body.splitlines(keepends=True)
            end = next(
                (
                    index
                    for index, line in enumerate(lines[1:], 1)
                    if line.strip() == "---"
                ),
                None,
            )
            if end is not None:
                body = "".join(lines[end + 1 :])
        base = skill.path.parent
        sampled_files = sorted(
            str(path.resolve())
            for path in base.rglob("*")
            if path.is_file() and path.name != "SKILL.md"
        )[:10]
        output = "\n".join(
            [
                f'<skill_content name="{skill.name}">',
                f"# Skill: {skill.name}",
                "",
                body.strip(),
                "",
                f"Base directory for this skill: {base}",
                "Relative paths in this skill (e.g., scripts/, reference/) are relative to this base directory.",
                "Note: file list is sampled.",
                "",
                "<skill_files>",
                *[f"<file>{path}</file>" for path in sampled_files],
                "</skill_files>",
                "</skill_content>",
            ]
        )
        return {
            "ok": True,
            "tool": "read_skill",
            "name": skill.name,
            "path": _relative(skill.path, self.workspace),
            "content": body,
            "sha256": skill.digest,
            "output": output,
            "files": sampled_files,
        }

    def _todowrite(self, args: dict[str, Any]) -> dict[str, Any]:
        todos = args.get("todos")
        if not isinstance(todos, list):
            return _error(
                "todowrite", "todos must be an array", error_type="invalid_arguments"
            )
        self._todos = [dict(item) for item in todos if isinstance(item, Mapping)]
        open_count = sum(
            1 for item in self._todos if str(item.get("status", "")) != "completed"
        )
        return {
            "ok": True,
            "tool": "todowrite",
            "todos": self._todos,
            "title": f"{open_count} todos",
            "output": json.dumps(self._todos, ensure_ascii=False, indent=2),
        }

    def _memory(self, args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        try:
            offset = max(int(args.get("offset", 0)), 0)
            limit = max(int(args.get("limit", 20)), 0)
        except (TypeError, ValueError):
            return _error(
                "memory_search",
                "offset and limit must be integers",
                error_type="invalid_arguments",
            )
        memory_cfg = self.policy.config.get("memory")
        memory_cfg = memory_cfg if isinstance(memory_cfg, Mapping) else {}
        if memory_cfg.get("enabled") is False:
            return {
                "ok": True,
                "tool": "memory_search",
                "query": query,
                "offset": offset,
                "limit": limit,
                "total": 0,
                "results": [],
                "output": "Memory search is disabled",
            }
        needle = query.casefold()
        hits: list[dict[str, Any]] = []
        # OpenCode's memory settings can select curated root files and a daily
        # memory directory. Keep discovery workspace-scoped even when the
        # general tool sandbox is disabled, so a memory query never expands
        # authority merely because ``sandbox.mode`` is off.
        curated = memory_cfg.get("curated_files", ("MEMORY.md", "USER.md"))
        if isinstance(curated, str):
            curated = (curated,)
        elif not isinstance(curated, (list, tuple, set)):
            curated = ("MEMORY.md", "USER.md")
        files: set[Path] = set()
        for name in curated:
            if not isinstance(name, str) or not name.strip():
                continue
            path = (self.workspace / name).resolve()
            if path.is_file() and path.is_relative_to(self.workspace):
                files.add(path)
        daily_dir = memory_cfg.get("daily_dir", "memory")
        if isinstance(daily_dir, str) and daily_dir.strip():
            daily = (self.workspace / daily_dir).resolve()
            if daily.is_dir() and daily.is_relative_to(self.workspace):
                for candidate in daily.rglob("*.md"):
                    try:
                        path = candidate.resolve()
                    except OSError:
                        continue
                    if path.is_file() and path.is_relative_to(self.workspace):
                        files.add(path)
        files = sorted(files)
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if needle and needle not in text.casefold():
                continue
            line = next(
                (
                    i
                    for i, value in enumerate(text.splitlines(), 1)
                    if not needle or needle in value.casefold()
                ),
                1,
            )
            lines = text.splitlines()
            start = max(line - 2, 0)
            end = min(start + 5, len(lines))
            hits.append(
                {
                    "path": _relative(path, self.workspace),
                    "line": line,
                    "snippet": "\n".join(lines[start:end]),
                    "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                }
            )
        # Session memory indexes are optional. Search text-bearing columns while
        # tolerating unrelated SQLite files and schema versions.
        for candidate in sorted(self.workspace.glob("**/*.sqlite")):
            try:
                dbpath = candidate.resolve()
            except OSError:
                continue
            if not dbpath.is_file() or not dbpath.is_relative_to(self.workspace):
                continue
            if dbpath.name == "session.sqlite" and dbpath.parent.name == ".opencode":
                continue
            try:
                with sqlite3.connect(dbpath) as db:
                    tables = [
                        r[0]
                        for r in db.execute(
                            "select name from sqlite_master where type='table'"
                        )
                    ]
                    for table in tables:
                        cols = [
                            r[1]
                            for r in db.execute(
                                f'pragma table_info("{table.replace(chr(34), chr(34) * 2)}")'
                            )
                        ]
                        text_col = next(
                            (
                                c
                                for c in ("content", "text", "body", "value")
                                if c in cols
                            ),
                            None,
                        )
                        if not text_col:
                            continue
                        rows = db.execute(
                            f'select "{text_col.replace(chr(34), chr(34) * 2)}" from "{table.replace(chr(34), chr(34) * 2)}"'
                        ).fetchall()
                        for (value,) in rows:
                            value = str(value or "")
                            if needle and needle not in value.casefold():
                                continue
                            hits.append(
                                {
                                    "path": _relative(dbpath, self.workspace),
                                    "table": table,
                                    "snippet": value[:1000],
                                    "sha256": hashlib.sha256(
                                        value.encode("utf-8")
                                    ).hexdigest(),
                                }
                            )
            except (OSError, sqlite3.Error):
                continue
        rendered = []
        for hit in hits[offset : offset + limit]:
            location = str(hit.get("path", ""))
            if hit.get("line"):
                location += f":{hit['line']}"
            rendered.append(f"{location}\n{hit.get('snippet', '')}")
        return {
            "ok": True,
            "tool": "memory_search",
            "query": query,
            "offset": offset,
            "limit": limit,
            "total": len(hits),
            "results": hits[offset : offset + limit],
            "output": "\n\n".join(rendered) if rendered else "No memory matches",
        }
