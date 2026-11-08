"""Scoped progress for interactive terminals and append-only experiment logs."""

from __future__ import annotations

import asyncio
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime

_SESSION = ContextVar("progress_session", default=None)
_SCOPE = ContextVar("progress_scope", default=())
_HEARTBEAT_SECONDS = 30
MODES = ("auto", "bar", "log", "off")


@contextmanager
def progress_session(mode="auto", *, stream=None):
    """Enable progress for this run only; child asyncio tasks inherit it."""
    if mode not in MODES:
        raise ValueError(f"run.progress must be one of {', '.join(MODES)}")
    stream = sys.stderr if stream is None else stream
    if mode == "auto":
        mode = "bar" if stream.isatty() else "log"
    token = _SESSION.set((mode, stream))
    scope_token = _SCOPE.set(())
    try:
        if mode == "bar":
            from tqdm.contrib.logging import logging_redirect_tqdm

            # Keep ordinary logging (including Harbor) above the live bars.
            with logging_redirect_tqdm():
                yield
        else:
            yield
    finally:
        _SCOPE.reset(scope_token)
        _SESSION.reset(token)


@contextmanager
def progress_scope(label):
    token = _SCOPE.set((*_SCOPE.get(), label))
    try:
        yield
    finally:
        _SCOPE.reset(token)


def progress_note(message):
    session = _SESSION.get()
    if session is None or session[0] == "off":
        return
    mode, stream = session
    label = " | ".join((*_SCOPE.get(), message))
    text = (
        f"PROGRESS {datetime.now().astimezone().isoformat(timespec='seconds')} {label}"
    )
    if mode == "bar":
        from tqdm import tqdm

        tqdm.write(text, file=stream)
    else:
        print(text, file=stream, flush=True)


def _duration(seconds):
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


class Progress:
    """Count completed work, not dispatched work; cancellation never fills a bar.

    Tasks must call start only after acquiring their concurrency slot, and
    finish after the entire environment/agent/verifier lifecycle has returned.
    No prompts, model configuration or exception strings enter this output.
    """

    def __init__(self, total, *, unit="tasks"):
        self.total = total
        self.unit = unit
        self.done = self.failed = self.cancelled = 0
        self.active = 0
        self.last = "-"
        self.label = " | ".join(_SCOPE.get()) or "rollout"
        self.mode, self.stream = _SESSION.get() or ("off", sys.stderr)
        self.bar = self.heartbeat = None

    async def __aenter__(self):
        self.started_at = time.monotonic()
        if self.mode == "bar":
            from tqdm import tqdm

            self.bar = tqdm(
                total=self.total,
                desc=self.label,
                unit=self.unit,
                file=self.stream,
                dynamic_ncols=True,
                mininterval=0.2,
                # Keep counts visible even when a narrow terminal truncates
                # the long round/candidate/repetition label and task name.
                bar_format=(
                    "{n_fmt}/{total_fmt} {unit} {percentage:3.0f}% |{bar:12}| "
                    "{desc} [{elapsed}<{remaining}] {postfix}"
                ),
            )
        self._emit("started")
        if self.mode != "off" and self.total:
            self.heartbeat = asyncio.create_task(self._heartbeat())
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        if self.heartbeat is not None:
            self.heartbeat.cancel()
            await asyncio.gather(self.heartbeat, return_exceptions=True)
        if exc_type is not None:
            self.cancelled += self.active
            self.active = 0
        state = "completed" if exc_type is None else "interrupted"
        self._emit(state)
        if self.bar is not None:
            self.bar.close()

    def start(self, item):
        self.active += 1
        self.last = f"{item}:running"
        if self.mode == "bar":
            self._emit("running")

    def finish(self, item, status="finished"):
        self.active -= 1
        if status == "cancelled":
            self.cancelled += 1
        else:
            self.done += 1
            self.failed += status == "error"
        self.last = f"{item}:{status}"
        self._emit("running")

    async def _heartbeat(self):
        while True:
            await asyncio.sleep(_HEARTBEAT_SECONDS)
            self._emit("running")

    def _emit(self, state):
        if self.mode == "off":
            return
        elapsed = time.monotonic() - self.started_at
        remaining = max(0, self.total - self.done - self.cancelled)
        eta = _duration(elapsed / self.done * remaining) if self.done else "?"
        active_key = "active_rounds" if self.unit == "rounds" else "active"
        queued_key = "queued_rounds" if self.unit == "rounds" else "queued"
        detail = (
            f"{active_key}={self.active} {queued_key}={remaining - self.active} "
            f"errors={self.failed} cancelled={self.cancelled} "
            f"elapsed={_duration(elapsed)} ETA~{eta} "
            f"state={state} last={self.last}"
        )
        if self.bar is not None:
            self.bar.set_postfix_str(detail, refresh=False)
            if not self.bar.update(self.done - self.bar.n):
                # Heartbeats and newly started tasks change the postfix even
                # when nothing has completed since tqdm's last refresh.
                self.bar.refresh()
        else:
            fraction = self.done / self.total if self.total else 1.0
            filled = int(20 * fraction)
            bar = "#" * filled + "-" * (20 - filled)
            stamp = datetime.now().astimezone().isoformat(timespec="seconds")
            print(
                f"PROGRESS {stamp} {self.label} | {self.unit} [{bar}] "
                f"{self.done}/{self.total} ({fraction:.0%}) {detail}",
                file=self.stream,
                flush=True,
            )
