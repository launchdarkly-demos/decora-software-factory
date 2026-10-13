import importlib

from factory.components import COMPONENTS


def test_ten_components_are_importable() -> None:
    assert len(COMPONENTS) == 10
    modules = {
        "intake": "factory.intake",
        "orchestrator": "factory.orchestrator",
        "agent_harness": "factory.harness",
        "specialist_agents": "factory.agents",
        "context": "factory.context",
        "tools": "factory.tools",
        "execution": "factory.execution",
        "verification": "factory.verification",
        "governance": "factory.governance",
        "observability": "factory.observability",
    }
    assert set(modules) == set(COMPONENTS)
    for name in COMPONENTS:
        importlib.import_module(modules[name])
