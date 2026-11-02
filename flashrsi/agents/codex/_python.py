"""Framework directories/configuration and JSONL mapping, outside the Snapshot."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import time
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from flashrsi._records import _redact, _secrets
from flashrsi.core import Trajectory


def _set(settings, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        settings = settings.setdefault(part, {})
    settings[parts[-1]] = value


def _memory_overlay(agent, home):
    """Persist explicit notes outside immutable resources, scoped by Snapshot."""
    if agent.config.get("_codex_evaluation") is True:
        return None
    configured = agent.config.get("memory_dir")
    if configured is None:
        return None
    store = Path(configured).expanduser().resolve()
    source = (
        Path(agent.config.get("source_dir", Path(__file__).parent))
        .expanduser()
        .resolve()
    )
    if store.is_relative_to(source) or store.is_relative_to(agent.files_dir):
        raise ValueError(
            "Codex memory_dir must be outside the Snapshot source directory"
        )
    store = store / agent.snapshot.id
    if store.is_symlink():
        raise ValueError("Codex Snapshot memory directory cannot be a symlink")
    if store.resolve().is_relative_to(source) or store.resolve().is_relative_to(
        agent.files_dir
    ):
        raise ValueError(
            "Codex memory_dir must be outside the Snapshot source directory"
        )
    notes = home / "memories/extensions/ad_hoc/notes"
    if store.exists():
        notes.mkdir(parents=True, exist_ok=True)
        for path in store.iterdir():
            if path.is_symlink() or not path.is_file():
                raise ValueError("Codex memory_dir may contain only regular note files")
            target = notes / path.name
            if target.exists() and target.read_bytes() != path.read_bytes():
                raise ValueError(
                    f"Runtime memory conflicts with selected Snapshot: {path.name}"
                )
            if not target.exists():
                shutil.copyfile(path, target)
    return store, notes


def _save_memory(overlay):
    if overlay is None:
        return
    store, notes = overlay
    if not notes.is_dir():
        return
    if store.is_symlink():
        raise ValueError("Codex Snapshot memory directory cannot be a symlink")
    store.mkdir(parents=True, exist_ok=True)
    for path in notes.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        target = store / path.name
        data = path.read_bytes()
        # Publish complete bytes without replacing another process's note.
        # Stage beside the per-Snapshot directory so readers cannot import it.
        with NamedTemporaryFile(
            prefix=".codex-note-", dir=store.parent, delete=False
        ) as output:
            temporary = Path(output.name)
            try:
                output.write(data)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or target.read_bytes() != data:
                    raise ValueError(
                        f"Conflicting append-only Codex memory note: {path.name}"
                    ) from None
        finally:
            temporary.unlink(missing_ok=True)


def _run_environment(agent, home, settings, env, deadline):
    """Describe inputs without importing the evolvable core into the host."""
    entries = []
    memory_bytes = 0
    memory = home / "memories"
    complete = True
    fingerprint_deadline = min(deadline, time.monotonic() + 0.05)
    try:
        if memory.is_symlink():
            raise OSError("memory root is a symlink")
        directories = [memory] if memory.is_dir() else []
        directory_index = total_entries = 0
        while directory_index < len(directories) and complete:
            with os.scandir(directories[directory_index]) as children:
                for entry in children:
                    total_entries += 1
                    if (
                        total_entries > 4096
                        or time.monotonic() >= fingerprint_deadline
                        or entry.is_symlink()
                    ):
                        complete = False
                        break
                    path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        directories.append(path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        complete = False
                        break
                    remaining = 16 * 1024 * 1024 - memory_bytes
                    if entry.stat(follow_symlinks=False).st_size > remaining:
                        complete = False
                        break
                    digest = hashlib.sha256()
                    with path.open("rb") as source:
                        while chunk := source.read(min(64 * 1024, remaining + 1)):
                            memory_bytes += len(chunk)
                            remaining -= len(chunk)
                            if (
                                remaining < 0
                                or time.monotonic() >= fingerprint_deadline
                            ):
                                complete = False
                                break
                            digest.update(chunk)
                    if not complete:
                        break
                    entries.append(
                        [path.relative_to(memory).as_posix(), digest.hexdigest()]
                    )
            directory_index += 1
    except OSError:
        complete = False
    initial_hash = (
        hashlib.sha256(
            json.dumps(
                sorted(entries), ensure_ascii=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        if complete
        else None
    )
    evaluating = agent.config.get("_codex_evaluation") is True
    provider_key = settings.get("model_provider")
    provider = settings.get("model_providers", {}).get(provider_key, {})
    dependencies = {}
    for name in (
        "flashrsi-lite",
        "httpx",
        "pillow",
        "tree-sitter",
        "tree-sitter-bash",
        "PyYAML",
    ):
        try:
            dependencies[name] = importlib.metadata.version(name)
        except Exception:
            dependencies[name] = None
    node_path = shutil.which("node", path=env.get("PATH"))
    return {
        "type": "CodexRunEnvironment",
        "schema_version": 1,
        "snapshot_id": agent.snapshot.id,
        "mode": "evaluation" if evaluating else "online",
        "runtime_memory_ignored": evaluating
        and agent.config.get("memory_dir") is not None,
        "initial_memory": {
            "source": "snapshot"
            if evaluating or agent.config.get("memory_dir") is None
            else "snapshot+runtime_notes",
            "sha256": initial_hash,
            "complete": complete,
            "files": len(entries),
            "bytes": memory_bytes,
            "fingerprint_limits": {
                "entries": 4096,
                "bytes": 16 * 1024 * 1024,
                "seconds": 0.05,
            },
        },
        "model": settings.get("model"),
        "provider": {"id": provider_key, "name": provider.get("name")},
        "reasoning_effort": settings.get("model_reasoning_effort"),
        "feature_overrides": copy.deepcopy(settings.get("features", {})),
        "sandbox": copy.deepcopy(settings.get("sandbox_mode")),
        "approval_policy": settings.get("approval_policy"),
        "budget": {
            "timeout_seconds": agent.config.get("timeout", 300),
            **{
                key: copy.deepcopy(value)
                for key, value in settings.items()
                if "budget" in key
                or key in {"model_context_window", "model_auto_compact_token_limit"}
            },
        },
        "environment": {
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            },
            "python": {
                "version": platform.python_version(),
                "implementation": platform.python_implementation(),
                "executable": sys.executable,
            },
            "node": {"path": node_path, "source": "PATH", "version": None},
            "shell": {"path": None, "source": "selected_core_event", "version": None},
            "configured_shell": settings.get("shell"),
            "environment_shell": env.get("SHELL"),
            "dependencies": dependencies,
        },
    }


async def _version(path, env, cwd):
    """Bounded, non-login version probe; diagnostics never become task errors."""
    from . import _stop

    if path is None:
        return None
    process = None
    try:
        async with asyncio.timeout(0.5):
            process = await asyncio.create_subprocess_exec(
                path,
                "--version",
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=os.name == "posix",
                limit=4096,
            )
            line = await process.stdout.readline()
            # Drain ordinary multiline version banners without storing them.
            while await process.stdout.read(4096):
                pass
            await process.wait()
            if process.returncode == 0:
                return line.decode(errors="replace").strip()[:1024] or None
    except (OSError, ValueError, TimeoutError):
        pass
    finally:
        if process is not None:
            # The probe's timeout can enter cleanup before core completion
            # cancels it again. Keep the process group cleanup independently
            # owned until it finishes, including across repeated cancellation.
            cleanup = asyncio.create_task(_stop(process, interrupt_timeout=0.1))
            cancelled = False
            while True:
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
                    if cleanup.done():
                        break
                except Exception:
                    break
                else:
                    break
            if cancelled:
                raise asyncio.CancelledError
    return None


async def run(agent, prompt, working_dir):
    from . import _runtime_home, _settings, _stop

    config = agent.config
    secrets = sorted(
        set(_secrets(os.environ) + _secrets(config)), key=len, reverse=True
    )
    messages = [
        {"type": "RunInput", "prompt": prompt, "snapshot_id": agent.snapshot.id}
    ]
    result, process, readers, diagnostic = None, None, [], []
    manifest = None
    probes = []
    event_error = None
    error = None
    deadline = asyncio.get_running_loop().time() + config.get("timeout", 300)

    async def probe_version(binary, env, cwd):
        path = binary["path"]
        try:
            version = await _version(path, env, cwd)
            if binary["path"] == path:
                binary["version"] = version
        except Exception:
            # Instrumentation must not change the task's result.
            pass

    async def events(stream):
        nonlocal result, event_error
        while True:
            try:
                line = await stream.readline()
            except ValueError:
                event_error = "Python Codex JSONL event exceeded the stream limit"
                while await stream.read(8192):
                    pass
                return
            if not line:
                return
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("event must be an object")
            except (ValueError, UnicodeError) as exc:
                event_error = f"Invalid Python Codex JSONL event: {exc}"
                continue
            messages.append(_redact(event, secrets))
            if event.get("type") == "codex.shell.selected" and manifest is not None:
                path = event.get("path")
                binary = manifest["environment"]["shell"]
                if isinstance(path, str) and path and binary["path"] != path:
                    binary["path"] = path
                    binary["version"] = None
                    if len(probes) < 16:
                        probes.append(
                            asyncio.create_task(
                                probe_version(binary, env, diagnostic_cwd)
                            )
                        )
            if event.get("type") == "flashrsi.core.completed":
                native_usage = event.get("usage", {})
                valid = (
                    event.get("status") in {"finished", "error", "interrupted"}
                    and isinstance(event.get("output", ""), str)
                    and isinstance(native_usage, dict)
                    and all(
                        type(value) is int and value >= 0
                        for value in native_usage.values()
                    )
                    and (
                        event.get("error_message") is None
                        or isinstance(event["error_message"], str)
                    )
                )
                if not valid:
                    event_error = "Invalid Python Codex completion fields"
                    result = {
                        "status": "error",
                        "output": "",
                        "usage": {},
                        "error_message": event_error,
                    }
                else:
                    result = event

    async def diagnostics(stream):
        while chunk := await stream.read(8192):
            diagnostic.append(chunk)

    with (
        TemporaryDirectory(prefix="flashrsi-codex-") as temporary,
        _runtime_home() as resource_temporary,
    ):
        root = Path(temporary).resolve()
        home = Path(resource_temporary).resolve() / "codex"
        diagnostic_cwd = root / "diagnostics"
        diagnostic_cwd.mkdir()
        memory_overlay = None
        cwd = Path(working_dir).resolve() if working_dir else root / "task"
        cwd.mkdir(parents=True, exist_ok=True)
        try:
            # Only the selected Snapshot files participate in execution.
            shutil.copytree(agent.files_dir, home)
            memory_overlay = _memory_overlay(agent, home)
            overrides, env, native = _settings(
                config,
                home,
                cwd,
                source_root=getattr(agent, "_container_source_dir", None),
            )
            settings = copy.deepcopy(native)
            for key, value in overrides.items():
                _set(settings, key, value)
            if config.get("_worker_system_prompt"):
                settings["developer_instructions"] = "\n\n".join(
                    filter(
                        None,
                        [
                            settings.get("developer_instructions"),
                            config["_worker_system_prompt"],
                        ],
                    )
                )
            settings["sandbox_mode"] = config.get(
                "sandbox", native.get("sandbox_mode", "workspace-write")
            )
            settings["model"] = config["llm"]["model_name"]
            request = {
                "prompt": prompt,
                "home": str(home),
                "cwd": str(cwd),
                "llm": dict(config["llm"]),
                "settings": settings,
                "environment": env,
            }
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            async with asyncio.timeout_at(deadline):
                manifest = _run_environment(agent, home, settings, env, deadline)
                messages.append(manifest)
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-I",
                    "-B",
                    str(Path(__file__).with_name("_entry.py")),
                    cwd=cwd,
                    env=env,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=os.name == "posix",
                    limit=16 * 1024 * 1024,
                )
                probes.append(
                    asyncio.create_task(
                        probe_version(
                            manifest["environment"]["node"], env, diagnostic_cwd
                        )
                    )
                )
                readers = [
                    asyncio.create_task(events(process.stdout)),
                    asyncio.create_task(diagnostics(process.stderr)),
                ]
                process.stdin.write(json.dumps(request).encode())
                await process.stdin.drain()
                process.stdin.close()
                await process.wait()
                await asyncio.gather(*readers)
                if probes:
                    await asyncio.wait(
                        probes,
                        timeout=min(
                            0.1,
                            max(0, deadline - asyncio.get_running_loop().time() - 0.02),
                        ),
                    )
            error = event_error
            if process.returncode:
                error = (
                    error or f"Python Codex process exited with {process.returncode}"
                )
            if result is None:
                error = error or "Python Codex ended without a completed turn"
            elif result.get("status") != "finished":
                error = (
                    error or result.get("error_message") or "Python Codex turn failed"
                )
        except TimeoutError:
            error = f"Codex timed out after {config.get('timeout', 300):g}s"
        except Exception as exc:
            error = str(exc) or type(exc).__name__
        finally:
            if process is not None:
                try:
                    await _stop(process, interrupt_timeout=15)
                except Exception as exc:
                    error = f"{error or ''}\nCodex cleanup failed: {exc}".strip()
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
            for probe in probes:
                if not probe.done():
                    probe.cancel()
            if probes:
                await asyncio.gather(*probes, return_exceptions=True)
            try:
                _save_memory(memory_overlay)
            except Exception as exc:
                error = f"{error or ''}\nCodex memory persistence failed: {exc}".strip()
    if diagnostic:
        text = b"".join(diagnostic).decode(errors="replace").rstrip()
        messages.append({"type": "CodexDiagnostics", "text": text})
        if error:
            error += "\nCodex stderr (tail):\n" + text[-8192:]
    native_usage = (result or {}).get("usage") or {}
    usage = {
        "prompt_tokens": native_usage.get("input_tokens", 0),
        "completion_tokens": native_usage.get("output_tokens", 0),
        "prompt_tokens_details": {
            "cached_tokens": native_usage.get("cached_input_tokens", 0)
        },
    }
    return Trajectory(
        status="error" if error else "finished",
        output=_redact((result or {}).get("output", ""), secrets) if not error else "",
        messages=_redact(messages, secrets),
        usage={} if error else usage,
        error_message=_redact(error, secrets),
    )
