"""Bounded recovery for Worker generation truncation, without changing budgets."""

import re

from flashrsi._progress import progress_note

_OUTPUT_LIMIT = re.compile(
    r"\b(?:Chat Completions|Responses) generation did not complete: "
    r"(?:length|max_output_tokens)\b"
)
_RETRY_NOTE = (
    "\nThe previous attempt exhausted a single response's output-token budget. "
    "This is a fresh attempt with the same model and limits. Keep deliberation "
    "brief, take concrete tool actions early, and return the required structured "
    "response promptly. Any candidate edits from that attempt were discarded; "
    "inspect the current files rather than assuming earlier actions persisted.\n"
)


class WorkerOutputBudgetExhausted(RuntimeError):
    """This phase used all of its allowed attempts after output truncation."""


async def retry_worker_phase(algorithm, phase, operation, *, step, variant=None):
    """Retry a whole phase so failed edits never leak into the next attempt.

    Agent's observer has already saved each failed trajectory and its usage.
    Only explicit generation-length errors are recoverable here; cancellation,
    context overflow, authentication and infrastructure failures still propagate.
    """
    attempts = algorithm.config.worker_output_retries + 1
    for attempt in range(attempts):
        try:
            return await operation(**({"retry_note": _RETRY_NOTE} if attempt else {}))
        except RuntimeError as error:
            if not _OUTPUT_LIMIT.search(str(error)):
                raise
            retrying = attempt + 1 < attempts
            action = "retry" if retrying else "skip"
            algorithm._record(
                "worker-output-budget",
                {
                    "step": step,
                    "variant": variant,
                    "phase": phase,
                    "attempt": attempt + 1,
                    "max_attempts": attempts,
                    "action": action,
                    "error": str(error),
                },
            )
            progress_note(
                f"Worker: phase={phase} output budget exhausted; "
                f"attempt={attempt + 1}/{attempts} action={action}"
            )
            if not retrying:
                raise WorkerOutputBudgetExhausted(
                    f"{phase} Worker output budget exhausted after {attempts} attempt(s)"
                ) from error
