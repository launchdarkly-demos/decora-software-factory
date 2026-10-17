class FactoryError(Exception):
    """A factory job stopped because of a rule or a bad request."""


class IncompleteBuildError(FactoryError):
    """Output was cut off; ask for a smaller part of the same task."""


class GovernanceError(FactoryError):
    """Policy refused a command, a path, or a publish."""


class RecipeError(FactoryError):
    """The builder has no safe change for this plan."""
