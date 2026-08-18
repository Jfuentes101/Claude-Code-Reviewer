"""What one PR's turn through a tick came to.

Here rather than in orchestrator.py so the phases that produce these — the queue,
the reply sweep, the CI watch — can be their own modules without importing the
thing that calls them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from robbie.config import Choice


@dataclass(frozen=True)
class Slot:
    """Room for one container — which arm runs it, or why nothing may."""

    ok: bool
    detail: str = ""
    choice: Choice = Choice()


@dataclass(frozen=True)
class Outcome:
    repo: str
    pr: int
    action: Literal[
        "review", "skip", "hold", "ci-note", "threads", "failed", "budget", "ready",
        "retract",
    ]
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.repo}#{self.pr} {self.action}" + (f" — {self.detail}" if self.detail else "")
