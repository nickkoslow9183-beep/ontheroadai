"""Common agent interfaces.

An agent is 'implemented' only if it has a class here that actually does the
work. Planned agents have a spec (so the dashboard can describe them) but no
implementation; the Orchestrator routes their tasks to a human instead.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

IMPLEMENTED = "implemented"
PARTIAL = "partial"
PLANNED = "planned"


@dataclass(frozen=True)
class AgentSpec:
    key: str
    name: str
    status: str  # implemented | partial | planned
    role: str
    works_today: tuple[str, ...] = ()
    planned: tuple[str, ...] = ()
    target_milestone: str = ""


@dataclass
class AgentResult:
    success: bool
    output: str
    problems: list[str] = field(default_factory=list)
    # True when the agent cannot finish *yet* because it needs data that has not
    # arrived (e.g. analytics before the export is imported). The task stays pending.
    waiting: bool = False
    # True when the agent did what it could but a person must finish the step.
    needs_human: bool = False


class Agent(ABC):
    spec: AgentSpec

    @abstractmethod
    def run(self, conn, task: dict, idea: dict) -> AgentResult:
        """Do the work for one task. Must not publish or call paid services."""
