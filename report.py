"""Progress output shared by the terminal CLI and the local agent behind the web UI.

The workflow calls ``report.get()`` for every message. The default reporter prints to the terminal (the CLI
output is unchanged); the agent installs a reporter per job that records stage events for the web UI.
"""

from __future__ import annotations

import contextvars
from typing import Any

# Stages shown in the web UI, in order.
STAGES = ("agent", "browser", "submit", "scan", "event", "generate", "validate", "render", "save")

_PREFIX = {"step": "\n", "info": "      ", "ok": "[OK] ", "warn": "[WARN] ", "fail": "[FAIL] ", "plain": ""}


class Reporter:
    """Terminal output."""

    def line(self, kind: str, text: str) -> None:
        print(_PREFIX[kind] + text)

    def stage(self, name: str, state: str, detail: str = "") -> None:
        """state: running | ok | warn | failed (the terminal shows the lines instead)."""

    def result(self, **data: Any) -> None:
        """Final data of a run (ZPL text, file paths, ...); only the agent uses it."""


_TERMINAL = Reporter()
_current: contextvars.ContextVar[Reporter | None] = contextvars.ContextVar("reporter", default=None)


def get() -> Reporter:
    return _current.get() or _TERMINAL


def use(reporter: Reporter) -> contextvars.Token:
    return _current.set(reporter)
