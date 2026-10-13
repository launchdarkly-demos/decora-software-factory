"""The ten components of the factory, in pipeline order."""

COMPONENTS: tuple[str, ...] = (
    "intake",
    "orchestrator",
    "agent_harness",
    "specialist_agents",
    "context",
    "tools",
    "execution",
    "verification",
    "governance",
    "observability",
)
