from __future__ import annotations

from factory.errors import FactoryError
from factory.models import Stage

# Legal edges. Verify and review both return to build, so this is a graph.
LEGAL: dict[Stage, frozenset[Stage]] = {
    Stage.INTAKE: frozenset({Stage.CONTEXT, Stage.FAILED}),
    Stage.CONTEXT: frozenset({Stage.PLAN, Stage.FAILED}),
    Stage.PLAN: frozenset({Stage.GOVERN_PLAN, Stage.FAILED}),
    Stage.GOVERN_PLAN: frozenset({Stage.PLAN, Stage.BUILD, Stage.AWAIT_HUMAN, Stage.FAILED}),
    Stage.AWAIT_HUMAN: frozenset({Stage.PLAN, Stage.BUILD, Stage.FAILED}),
    Stage.BUILD: frozenset({Stage.VERIFY, Stage.BUILD, Stage.AWAIT_HUMAN, Stage.FAILED}),
    Stage.VERIFY: frozenset({Stage.ENSURE_FLAG, Stage.BUILD, Stage.AWAIT_HUMAN, Stage.FAILED}),
    Stage.ENSURE_FLAG: frozenset({Stage.REVIEW, Stage.FAILED}),
    Stage.REVIEW: frozenset({Stage.GOVERN_SHIP, Stage.BUILD, Stage.AWAIT_HUMAN, Stage.FAILED}),
    Stage.GOVERN_SHIP: frozenset({Stage.DRAFT_PR, Stage.BUILD, Stage.AWAIT_HUMAN, Stage.FAILED}),
    Stage.DRAFT_PR: frozenset({Stage.DONE, Stage.FAILED}),
    Stage.DONE: frozenset(),
    Stage.FAILED: frozenset(),
}

COMPONENTS = {
    Stage.INTAKE: "intake",
    Stage.CONTEXT: "context",
    Stage.PLAN: "specialist_agents",
    Stage.GOVERN_PLAN: "governance",
    Stage.AWAIT_HUMAN: "governance",
    Stage.BUILD: "specialist_agents",
    Stage.VERIFY: "verification",
    Stage.ENSURE_FLAG: "governance",
    Stage.REVIEW: "specialist_agents",
    Stage.GOVERN_SHIP: "governance",
    Stage.DRAFT_PR: "orchestrator",
    Stage.DONE: "observability",
    Stage.FAILED: "observability",
}


def assert_legal(source: Stage, dest: Stage) -> None:
    if dest not in LEGAL[source]:
        raise FactoryError(f"illegal transition {source.value} -> {dest.value}")
