"""Built-in harness adapters; selection by configuration belongs to the runner."""

from flashrsi.agents.claude_code import ClaudeCode
from flashrsi.agents.codex import Codex
from flashrsi.agents.hermes import Hermes
from flashrsi.agents.llm import LLM
from flashrsi.agents.miniagent import MiniAgent
from flashrsi.agents.openclaw import OpenClaw
from flashrsi.agents.opencode import OpenCode
from flashrsi.agents.pi_agent import Pi, PiAgent

__all__ = [
    "ClaudeCode",
    "Codex",
    "Hermes",
    "LLM",
    "MiniAgent",
    "OpenClaw",
    "OpenCode",
    "Pi",
    "PiAgent",
]
