class FactoryError(Exception):
    """A factory job stopped because of a rule or a bad request."""


class DestructiveEditError(FactoryError):
    """A staged proposal would discard most of an existing file."""


class IncompleteBuildError(FactoryError):
    """Output was cut off; ask for a smaller part of the same task."""


class GovernanceError(FactoryError):
    """Policy refused a command, a path, or a publish."""


class SafetyError(GovernanceError):
    """Fatal Builder safety violation (E16 or E17)."""


class RecipeError(FactoryError):
    """The builder has no safe change for this plan."""


class InfrastructureError(FactoryError):
    """Retry provider transport failures without consuming task budgets."""


class ProviderTransientError(InfrastructureError):
    """A transient provider limit, with a safe code and optional retry delay."""

    def __init__(self, code: str, retry_after: float | None = None) -> None:
        self.code = code
        self.retry_after = retry_after
        super().__init__(f"Provider infrastructure error: {code}")


class ProviderBlockedError(FactoryError):
    """Account credentials or billing must be repaired before resuming."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Provider billing/credentials problem: {code}. "
                         "Fix the OpenAI account, then answer continue (or add factory:retry).")
